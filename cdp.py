"""A minimal Chrome DevTools Protocol client, no third-party dependencies.

Why hand-rolled: a visual-regression plugin that needs Playwright, Node or a 500MB browser
bundle is a plugin that fails to install on the judge's machine. Chrome is already on every
box Hermes targets, and CDP over a local WebSocket is about 200 lines of stdlib.

Covers exactly what this plugin needs: launch, navigate, evaluate, screenshot, device metrics.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/microsoft-edge",
    "/snap/bin/chromium",
]


class CDPError(RuntimeError):
    pass


# The browser gets only what it needs to start and draw. Everything else in the Hermes process
# environment, provider API keys included, stays out of the child process.
_ENV_KEEP = frozenset({
    "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TMP", "TEMP",
    "DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR",
    # Windows locates its own system folders and the user profile through these.
    "SYSTEMROOT", "WINDIR", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA",
    "PROGRAMFILES", "PROGRAMFILES(X86)",
})


def browser_env(source: Optional[dict] = None) -> dict:
    source = os.environ if source is None else source
    return {key: value for key, value in source.items() if key.upper() in _ENV_KEEP}


class NavigationError(CDPError):
    """The page never really loaded: refused connection, DNS failure, or an HTTP error status."""


# Stops animation for a stable screenshot. Finite animations jump to their end state so a fade-in
# is captured visible; infinite ones rest on their first frame.
_FREEZE_JS = r"""
(() => {
  const style = document.createElement('style');
  style.setAttribute('data-visual-gate', 'freeze');
  style.textContent = '*,*::before,*::after{transition:none!important;caret-color:transparent!important}';
  (document.head || document.documentElement).appendChild(style);
  for (const anim of document.getAnimations()) {
    try {
      const timing = anim.effect && anim.effect.getComputedTiming();
      if (timing && timing.iterations !== Infinity && timing.endTime !== Infinity) anim.finish();
      else { anim.pause(); anim.currentTime = 0; }
    } catch (err) { /* a detached animation has nothing to freeze */ }
  }
  return 1;
})()
"""


def find_chrome(explicit: str = "") -> Optional[str]:
    """Locate a usable Chromium-family binary, or None."""
    if explicit:
        p = Path(explicit).expanduser()
        if p.exists():
            return str(p)
    env = os.environ.get("VISUAL_GATE_CHROME") or os.environ.get("CHROME_PATH")
    if env and Path(env).expanduser().exists():
        return str(Path(env).expanduser())
    for cand in _CANDIDATES:
        if Path(cand).exists():
            return cand
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    return None


# ── WebSocket (RFC 6455, client side, text frames only) ────────────────────────

class _WS:
    def __init__(self, url: str, timeout: float = 30.0) -> None:
        assert url.startswith("ws://"), url
        rest = url[5:]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CDPError("websocket handshake: connection closed")
            buf += chunk
        head = buf.split(b"\r\n\r\n", 1)[0].decode("latin-1")
        if "101" not in head.split("\r\n")[0]:
            raise CDPError(f"websocket handshake rejected: {head.splitlines()[0]}")
        self._rest = buf.split(b"\r\n\r\n", 1)[1]
        self._buf = b""

    def _recv_exact(self, n: int) -> bytes:
        while len(self._rest) < n:
            chunk = self.sock.recv(max(4096, n - len(self._rest)))
            if not chunk:
                raise CDPError("websocket closed")
            self._rest += chunk
        out, self._rest = self._rest[:n], self._rest[n:]
        return out

    def send(self, payload: str) -> None:
        data = payload.encode("utf-8")
        header = bytearray([0x81])
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(header) + masked)

    def recv(self) -> str:
        while True:
            b0, b1 = self._recv_exact(2)
            fin = b0 & 0x80
            opcode = b0 & 0x0F
            masked = b1 & 0x80
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else b""
            payload = self._recv_exact(length) if length else b""
            if masked:
                payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
            if opcode == 0x8:              # close
                raise CDPError("websocket closed by peer")
            if opcode == 0x9:              # ping -> pong
                self._control(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2, 0x0):
                self._buf += payload
                if not fin:
                    continue          # a fragmented message ends on the frame with FIN set
                text = self._buf.decode("utf-8")
                self._buf = b""
                return text

    def _control(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        head = bytes([0x80 | opcode, 0x80 | len(payload)]) + mask
        self.sock.sendall(head + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def close(self) -> None:
        try:
            self._control(0x8, b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


# ── browser session ────────────────────────────────────────────────────────────

@dataclass
class Shot:
    png: bytes
    width: int
    height: int


class Browser:
    """Context manager: launch Chrome headless, attach to one target, drive it."""

    def __init__(self, chrome: str, *, width: int = 1280, height: int = 800,
                 device_scale: float = 1.0, headless: bool = True,
                 file_guard: Optional[Callable[[str], bool]] = None) -> None:
        self.chrome = chrome
        self.file_guard = file_guard
        self.width = width
        self.height = height
        self.device_scale = device_scale
        self.headless = headless
        self._proc: Optional[subprocess.Popen] = None
        self._profile: Optional[str] = None
        self._ws: Optional[_WS] = None
        self._id = 0
        self._session: Optional[str] = None
        self._events: list[dict] = []
        self.blocked_files: list[str] = []

    def __enter__(self) -> "Browser":
        self._profile = tempfile.mkdtemp(prefix="visual-gate-")
        args = [
            self.chrome,
            "--remote-debugging-port=0",
            f"--user-data-dir={self._profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-sync",
            "--disable-default-apps",
            "--mute-audio",
            "--hide-scrollbars",
            "--force-device-scale-factor=%s" % self.device_scale,
            f"--window-size={self.width},{self.height}",
            "--disable-dev-shm-usage",
        ]
        if os.name == "posix" and os.geteuid() == 0:
            args.append("--no-sandbox")       # Chrome refuses to start as root, as in most containers
        if self.headless:
            args.append("--headless=new")
        args.append("about:blank")
        creation = 0x08000000 if os.name == "nt" else 0     # CREATE_NO_WINDOW
        self._proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, env=browser_env(),
                                      stderr=subprocess.DEVNULL, creationflags=creation)
        port = self._wait_port()
        ws_url = self._browser_ws(port)
        self._ws = _WS(ws_url)
        target = self._call("Target.createTarget", {"url": "about:blank"})
        attached = self._call("Target.attachToTarget",
                              {"targetId": target["targetId"], "flatten": True})
        self._session = attached["sessionId"]
        self._call("Page.enable", {})
        self._call("Runtime.enable", {})
        self._call("Network.enable", {})
        if self.file_guard is not None:
            # Every file:// load, the page itself or anything it embeds, is held until judged.
            self._call("Fetch.enable", {"patterns": [{"urlPattern": "file://*"}]})
        self._call("Emulation.setDeviceMetricsOverride", {
            "width": self.width, "height": self.height,
            "deviceScaleFactor": self.device_scale, "mobile": False,
        })
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            if self._ws:
                self._ws.close()
        finally:
            if self._proc:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=10)
                except Exception:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass
            if self._profile:
                shutil.rmtree(self._profile, ignore_errors=True)

    # -- internals ------------------------------------------------------------

    def _wait_port(self, timeout: float = 20.0) -> int:
        assert self._profile
        marker = Path(self._profile) / "DevToolsActivePort"
        deadline = time.time() + timeout
        while time.time() < deadline:
            if marker.exists():
                try:
                    first = marker.read_text(encoding="utf-8").splitlines()[0].strip()
                    if first.isdigit():
                        return int(first)
                except (OSError, IndexError):
                    pass
            if self._proc and self._proc.poll() is not None:
                raise CDPError("Chrome exited before exposing a DevTools port")
            time.sleep(0.05)
        raise CDPError("Chrome did not open a DevTools port in time")

    @staticmethod
    def _browser_ws(port: int) -> str:
        import urllib.request
        for _ in range(80):
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/json/version", timeout=2) as resp:
                    info = json.loads(resp.read().decode())
                url = info.get("webSocketDebuggerUrl")
                if url:
                    return url
            except Exception:
                time.sleep(0.1)
        raise CDPError("could not read /json/version from Chrome")

    def _call(self, method: str, params: Optional[dict] = None, *,
              timeout: float = 45.0) -> dict:
        if self._ws is None:
            raise CDPError("browser not started")
        self._id += 1
        mid = self._id
        message: dict[str, Any] = {"id": mid, "method": method, "params": params or {}}
        if self._session and not method.startswith("Target."):
            message["sessionId"] = self._session
        self._ws.send(json.dumps(message))
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._ws.sock.settimeout(max(0.1, deadline - time.time()))
            try:
                raw = self._ws.recv()
            except socket.timeout:
                break
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("id") == mid:
                if "error" in msg:
                    raise CDPError(f"{method}: {msg['error'].get('message')}")
                return msg.get("result") or {}
            self._note_event(msg)
        raise CDPError(f"{method}: timed out after {timeout}s")

    def _note_event(self, msg: dict) -> None:
        """Keep only what navigation reads, so a chatty page cannot grow this list without bound."""
        method = msg.get("method")
        if method == "Fetch.requestPaused":
            self._judge_file_request(msg.get("params") or {})
        elif method == "Page.loadEventFired":
            self._events.append(msg)
        elif method == "Network.responseReceived" and msg.get("params", {}).get("type") == "Document":
            self._events.append(msg)

    def _judge_file_request(self, params: dict) -> None:
        """Let a paused file:// request through only if the guard allows it. The reply is sent
        without waiting for its acknowledgement, so it can be issued from inside another call."""
        url = str((params.get("request") or {}).get("url") or "")
        allowed = bool(self.file_guard and self.file_guard(url))
        method = "Fetch.continueRequest" if allowed else "Fetch.failRequest"
        payload: dict[str, Any] = {"requestId": params.get("requestId")}
        if not allowed:
            payload["errorReason"] = "AccessDenied"
            self.blocked_files.append(url)
        self._id += 1
        message = {"id": self._id, "method": method, "params": payload}
        if self._session:
            message["sessionId"] = self._session
        if self._ws:
            self._ws.send(json.dumps(message))

    def _drain_until(self, event: str, timeout: float = 30.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._ws:
                self._ws.sock.settimeout(max(0.1, deadline - time.time()))
            for i, ev in enumerate(self._events):
                if ev.get("method") == event:
                    return self._events.pop(i)
            try:
                raw = self._ws.recv() if self._ws else ""
            except socket.timeout:
                break
            if not raw:
                break
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("method") == event:
                return msg
            self._note_event(msg)
        raise CDPError(f"event {event} not seen within {timeout}s")

    # -- public ---------------------------------------------------------------

    def _document_status(self, frame_id: str) -> Optional[int]:
        """HTTP status of the main document, or None for file:, data: and about: pages."""
        status = None
        for ev in self._events:
            params = ev.get("params", {})
            if ev.get("method") == "Network.responseReceived" and params.get("frameId") == frame_id:
                status = (params.get("response") or {}).get("status")
        return int(status) if status else None

    def navigate(self, url: str, *, wait: float = 0.35, settle: float = 0.9,
                 allow_error_status: bool = False, freeze: bool = True) -> Optional[int]:
        """Load ``url`` and return its HTTP status. A page that failed to load raises, because
        Chrome renders its own error page and that must never be judged as the user's page."""
        self._events.clear()
        started = self._call("Page.navigate", {"url": url}, timeout=60)
        if started.get("errorText"):
            raise NavigationError(f"could not load {url}: {started['errorText']}")
        try:
            self._drain_until("Page.loadEventFired", timeout=30)
        except CDPError:
            pass                              # a page that never fires load still renders
        status = self._document_status(str(started.get("frameId") or ""))
        if status and status >= 400 and not allow_error_status:
            raise NavigationError(f"{url} answered HTTP {status}; set allow_error_status=true "
                                  f"if this error page is what you mean to check")
        self.evaluate("document.fonts && document.fonts.ready ? "
                      "document.fonts.ready.then(()=>1) : 1", await_promise=True)
        time.sleep(max(0.0, wait))
        if freeze:
            self.evaluate(_FREEZE_JS)
        self.evaluate("window.scrollTo(0,0)")
        time.sleep(max(0.0, settle * 0.2))
        return status

    def evaluate(self, expression: str, *, await_promise: bool = False,
                 timeout: float = 60.0) -> Any:
        result = self._call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
            "userGesture": False,
            "timeout": int(timeout * 1000),
        }, timeout=timeout + 5)
        if result.get("exceptionDetails"):
            detail = result["exceptionDetails"]
            text = (detail.get("exception") or {}).get("description") or detail.get("text")
            raise CDPError(f"page script failed: {text}")
        return (result.get("result") or {}).get("value")

    def screenshot(self, *, full_page: bool = False) -> Shot:
        params: dict[str, Any] = {"format": "png", "captureBeyondViewport": bool(full_page)}
        if full_page:
            metrics = self._call("Page.getLayoutMetrics")
            size = metrics.get("cssContentSize") or metrics.get("contentSize") or {}
            width = int(size.get("width") or self.width)
            height = int(size.get("height") or self.height)
            params["clip"] = {"x": 0, "y": 0, "width": width, "height": height, "scale": 1}
        result = self._call("Page.captureScreenshot", params, timeout=90)
        data = base64.b64decode(result.get("data") or "")
        if not data:
            raise CDPError("Chrome returned an empty screenshot")
        from PIL import Image
        import io
        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size
        return Shot(png=data, width=width, height=height)

    def viewport_pixels(self) -> tuple[int, int]:
        result = self.evaluate("[window.innerWidth, window.innerHeight]")
        if isinstance(result, list) and len(result) == 2:
            return int(result[0]), int(result[1])
        return self.width, self.height
