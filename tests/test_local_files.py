"""Local file policy: what a page or image path may reach, and what Chrome inherits.

Page text and screenshots go back to the agent, so local files get the same gate as Hermes'
read_file: web page types only, inside the session working directory, never a secret file.
"""

import io
import json
import os
import sys
import types

import pytest
from PIL import Image
from visual_gate import cdp, tools


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    work = tmp_path / "work"
    outside = tmp_path / "outside"
    work.mkdir()
    outside.mkdir()
    monkeypatch.chdir(work)
    (work / "page.html").write_text("<!doctype html><html lang=en><head><title>p</title>"
                                    "<link rel=stylesheet href=style.css></head><body><h1>Inside</h1>"
                                    "<iframe id=out src='../outside/secret.html'></iframe>"
                                    "<iframe id=env src='.env'></iframe></body></html>")
    (work / "style.css").write_text("h1{color:#123456}")
    (work / ".env").write_text("SECRET_KEY=abc")
    (work / "notes.txt").write_text("plain text")
    (outside / "secret.html").write_text("<p>TOP SECRET OUTSIDE</p>")
    return work, outside


def png(path, color):
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), color).save(buf, format="PNG")
    path.write_bytes(buf.getvalue())
    return path


# ---- which local pages are accepted ---------------------------------------------------------

def test_page_inside_the_working_directory_is_accepted(workspace):
    work, _ = workspace
    for given in ("page.html", str(work / "page.html"), (work / "page.html").as_uri()):
        assert tools.normalise_url(given) == (work / "page.html").as_uri()


@pytest.mark.parametrize("given", [
    "../outside/secret.html",        # outside the working directory
    ".env",                          # secret file
    "notes.txt",                     # not a web page
    "style.css",                     # an asset, not a page
    "missing.html",
])
def test_unsafe_local_paths_are_refused(workspace, given):
    with pytest.raises((PermissionError, FileNotFoundError)):
        tools.normalise_url(given)


def test_file_url_outside_the_working_directory_is_refused(workspace):
    _, outside = workspace
    with pytest.raises(PermissionError):
        tools.normalise_url((outside / "secret.html").as_uri())


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need extra rights on Windows")
def test_symlink_out_of_the_working_directory_is_refused(workspace):
    work, outside = workspace
    (work / "link.html").symlink_to(outside / "secret.html")
    with pytest.raises(PermissionError):
        tools.normalise_url("link.html")


@pytest.mark.parametrize("given", ["javascript:alert(1)", "chrome://settings", "about:config",
                                   "view-source:http://example.com", "ftp://example.com/x"])
def test_other_schemes_are_refused(workspace, given):
    with pytest.raises(PermissionError, match="only http"):
        tools.normalise_url(given)


def test_web_urls_are_unchanged(workspace):
    assert tools.normalise_url("https://example.com") == "https://example.com"
    assert tools.normalise_url("localhost:3000") == "http://localhost:3000"
    assert tools.normalise_url("about:blank") == "about:blank"


def test_hermes_read_guard_is_honoured(workspace, monkeypatch):
    """A path Hermes refuses for read_file is refused here, even inside the working directory."""
    blocked = types.ModuleType("agent.file_safety")
    blocked.get_read_block_error = lambda path: (
        "Access denied: credential store" if path.endswith("page.html") else None)
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.file_safety", blocked)
    with pytest.raises(PermissionError, match="credential store"):
        tools.normalise_url("page.html")


def test_session_working_directory_comes_from_hermes(workspace, monkeypatch, tmp_path):
    elsewhere = tmp_path / "session"
    elsewhere.mkdir()
    (elsewhere / "app.html").write_text("<h1>x</h1>")
    runtime = types.ModuleType("agent.runtime_cwd")
    runtime.resolve_agent_cwd = lambda: elsewhere
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.runtime_cwd", runtime)
    assert tools.normalise_url("app.html") == (elsewhere / "app.html").resolve().as_uri()
    with pytest.raises(PermissionError):
        tools.normalise_url(str(workspace[0] / "page.html"))


# ---- vg_diff paths --------------------------------------------------------------------------

def test_diff_accepts_images_in_the_working_directory(workspace):
    work, _ = workspace
    png(work / "a.png", "white")
    png(work / "b.png", "white")
    result = json.loads(tools.vg_diff({"baseline": "a.png", "actual": "b.png", "report": False}))
    assert result["ok"] is True and result["passed"] is True


def test_diff_refuses_paths_outside_or_non_images(workspace):
    work, outside = workspace
    png(outside / "x.png", "white")
    png(work / "a.png", "white")
    for baseline in (str(outside / "x.png"), ".env", "notes.txt"):
        result = json.loads(tools.vg_diff({"baseline": baseline, "actual": "a.png"}))
        assert result["ok"] is False


# ---- the browser process --------------------------------------------------------------------

def test_browser_environment_drops_secrets():
    env = cdp.browser_env({"PATH": "/bin", "HOME": "/h", "LANG": "C", "OPENROUTER_API_KEY": "sk-1",
                           "ANTHROPIC_API_KEY": "sk-2", "GITHUB_TOKEN": "ghp", "HERMES_HOME": "/x",
                           "DISPLAY": ":0", "XDG_RUNTIME_DIR": "/run/user/1"})
    assert env == {"PATH": "/bin", "HOME": "/h", "LANG": "C", "DISPLAY": ":0",
                   "XDG_RUNTIME_DIR": "/run/user/1"}


@pytest.mark.skipif(not cdp.find_chrome(), reason="needs Chrome, Edge or Chromium")
def test_chrome_is_started_with_the_filtered_environment(monkeypatch):
    monkeypatch.setenv("FAKE_PROVIDER_API_KEY", "sk-must-not-reach-chrome")
    seen = {}
    real_popen = cdp.subprocess.Popen

    def spy(*args, **kwargs):
        seen["env"] = kwargs.get("env")
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(cdp.subprocess, "Popen", spy)
    with tools.Session(400, 300, 1.0) as browser:
        assert browser.evaluate("1+1") == 2
    assert seen["env"] is not None
    assert "FAKE_PROVIDER_API_KEY" not in seen["env"]


@pytest.mark.skipif(not cdp.find_chrome(), reason="needs Chrome, Edge or Chromium")
def test_page_cannot_embed_files_outside_the_policy(workspace):
    work, _ = workspace
    result = json.loads(tools.vg_a11y({"url": "page.html", "report": False}))
    assert result["ok"] is True
    with tools.Session(800, 600, 1.0) as browser:
        tools._open(browser, tools.normalise_url("page.html"), {})
        assert browser.evaluate("getComputedStyle(document.querySelector('h1')).color") \
            == "rgb(18, 52, 86)"                          # its own stylesheet still loads
        blocked = [url.rsplit("/", 1)[-1] for url in browser.blocked_files]
    assert "secret.html" in blocked and ".env" in blocked
