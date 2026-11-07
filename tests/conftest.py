"""Load the plugin as a package and give it a throwaway Hermes home.

The plugin directory name has a dash, so it cannot be imported by name. Inside a real Hermes
checkout the real ``hermes_constants`` wins; outside one, a small stand-in is used.
"""

import http.server
import importlib.util
import os
import pathlib
import socketserver
import sys
import threading
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_plugin():
    spec = importlib.util.spec_from_file_location(
        "visual_gate", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["visual_gate"] = module
    spec.loader.exec_module(module)
    return module


if "hermes_constants" not in sys.modules:
    try:
        import hermes_constants  # noqa: F401
    except ImportError:
        stub = types.ModuleType("hermes_constants")
        stub.get_hermes_home = lambda: pathlib.Path(os.environ["HERMES_HOME"])
        sys.modules["hermes_constants"] = stub

_load_plugin()


@pytest.fixture(autouse=True)
def hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


class _Pages:
    def __init__(self, port):
        self.port = port
        self.pages = {}

    def add(self, key, body, status=200, css="", title="t"):
        self.pages[key] = (status, (
            f"<!doctype html><html lang=en><head><meta charset=utf-8><title>{title}</title>"
            f"<style>body{{margin:24px;font:16px Arial;background:#fff;color:#111}}{css}</style>"
            f"</head><body>{body}</body></html>"))
        return f"http://127.0.0.1:{self.port}/{key}"


@pytest.fixture(scope="module")
def site():
    pages = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            status, html = pages.get(self.path.strip("/"), (404, "<h1>Not found</h1>"))
            body = html.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

        def handle(self):
            try:
                super().handle()
            except ConnectionError:
                pass

    class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

    server = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    helper = _Pages(server.server_address[1])
    helper.pages = pages
    yield helper
    server.shutdown()
