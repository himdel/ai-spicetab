#!/usr/bin/env python3
"""Serve a read-only view of the current X desktop in the browser."""

import http.server
import io
import mimetypes
import os
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).parent
NOVNC_VERSION = "1.4.0"
NOVNC_DIR = ROOT / ".novnc"
DISPLAY = os.environ.get("DISPLAY", ":0")
IDLE_TIMEOUT = 300

_last_ping = time.time()
_procs: list[subprocess.Popen] = []
_httpd = None
_stopping = threading.Event()


HTML = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>spicetab</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:100%%;height:100%%;overflow:hidden;background:#222}
#screen{width:100%%;height:100%%}
#msg{color:#888;font:15px/1.4 system-ui,sans-serif;text-align:center;padding-top:45vh}
</style>
</head>
<body>
<div id="screen"><p id="msg">Connecting…</p></div>
<script type="module">
import RFB from './novnc/core/rfb.js';
document.getElementById('msg')?.remove();
const rfb = new RFB(document.getElementById('screen'),
                    'ws://' + location.hostname + ':%d');
rfb.scaleViewport = true;
rfb.resizeSession = false;
rfb.viewOnly = true;
rfb.background = '#222';
rfb.addEventListener('disconnect', e => {
  document.getElementById('screen').innerHTML =
    '<p id="msg">' + (e.detail.clean ? 'Disconnected.' : 'Connection lost.') + '</p>';
});
setInterval(() => fetch('/ping').catch(() => {}), 15000);
fetch('/ping');
</script>
</body>
</html>
"""


def _free_port():
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _ensure_novnc():
    if (NOVNC_DIR / "core" / "rfb.js").exists():
        return
    url = (
        "https://github.com/novnc/noVNC/archive/"
        f"refs/tags/v{NOVNC_VERSION}.tar.gz"
    )
    print(f"Downloading noVNC v{NOVNC_VERSION}...")
    try:
        with urllib.request.urlopen(url) as resp:
            data = resp.read()
    except urllib.error.URLError as e:
        sys.exit(f"Failed to download noVNC: {e}")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        try:
            tar.extractall(path=ROOT, filter="data")
        except TypeError:
            tar.extractall(path=ROOT)
    extracted = ROOT / f"noVNC-{NOVNC_VERSION}"
    if NOVNC_DIR.exists():
        shutil.rmtree(NOVNC_DIR)
    extracted.rename(NOVNC_DIR)
    print("noVNC ready.")


def _handler_class(ws_port):
    page = (HTML % ws_port).encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            global _last_ping
            path = urllib.parse.urlparse(self.path).path

            if path == "/ping":
                _last_ping = time.time()
                self.send_response(204)
                self.end_headers()
                return

            if path in ("", "/", "/index.html"):
                self._send(page, "text/html;charset=utf-8")
                return

            if path.startswith("/novnc/"):
                rel = urllib.parse.unquote(path[7:])
                fp = (NOVNC_DIR / rel).resolve()
                if (
                    str(fp).startswith(str(NOVNC_DIR.resolve()))
                    and fp.is_file()
                ):
                    ct = (
                        mimetypes.guess_type(fp.name)[0]
                        or "application/octet-stream"
                    )
                    self._send(fp.read_bytes(), ct)
                    return

            self.send_error(404)

        def _send(self, body: bytes, content_type: str):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a):
            pass

    return Handler


def _cleanup():
    _stopping.set()
    for p in _procs:
        try:
            p.terminate()
        except OSError:
            pass
    if _httpd:
        threading.Thread(target=_httpd.shutdown, daemon=True).start()
    deadline = time.time() + 3
    for p in _procs:
        try:
            p.wait(timeout=max(0.1, deadline - time.time()))
        except subprocess.TimeoutExpired:
            p.kill()


def _watchdog():
    while not _stopping.wait(10):
        if time.time() - _last_ping > IDLE_TIMEOUT:
            print(f"\nNo activity for {IDLE_TIMEOUT}s, exiting.")
            os.kill(os.getpid(), signal.SIGINT)
            return


def main():
    global _httpd

    _ensure_novnc()

    vnc_port = _free_port()
    ws_port = _free_port()
    http_port = _free_port()

    # x11vnc
    try:
        x11vnc = subprocess.Popen(
            [
                "x11vnc", "-display", DISPLAY, "-viewonly", "-shared",
                "-forever", "-nopw", "-rfbport", str(vnc_port), "-q",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        sys.exit("x11vnc not found — install it (e.g. apt install x11vnc)")
    _procs.append(x11vnc)
    time.sleep(0.5)
    if x11vnc.poll() is not None:
        sys.exit("x11vnc failed to start")

    # websockify
    ws_bin = shutil.which("websockify")
    ws_cmd = (
        [ws_bin] if ws_bin else [sys.executable, "-m", "websockify"]
    )
    ws_cmd += [str(ws_port), f"localhost:{vnc_port}"]
    try:
        wsproxy = subprocess.Popen(
            ws_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except FileNotFoundError:
        _cleanup()
        sys.exit("websockify not found")
    _procs.append(wsproxy)
    time.sleep(0.3)
    if wsproxy.poll() is not None:
        _cleanup()
        sys.exit("websockify failed to start")

    # HTTP server
    _httpd = http.server.ThreadingHTTPServer(
        ("127.0.0.1", http_port), _handler_class(ws_port)
    )
    threading.Thread(target=_httpd.serve_forever, daemon=True).start()
    threading.Thread(target=_watchdog, daemon=True).start()

    url = f"http://localhost:{http_port}"
    print(f"\n  {url}\n")
    print("Ctrl+C to stop  ·  auto-exits after 5 min idle\n")
    webbrowser.open(url)

    try:
        while not _stopping.is_set():
            for p in _procs:
                if p.poll() is not None:
                    return
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        _cleanup()


if __name__ == "__main__":
    main()
