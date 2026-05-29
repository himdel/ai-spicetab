#!/usr/bin/env python3
"""Serve a read-only view of the current X desktop in the browser."""

import http.server
import io
import json
import mimetypes
import os
import re
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

_ENV_PREFIX = "_SPICETAB_"
_SCRIPT = Path(__file__).resolve()

_last_ping = time.time()
_procs: list = []
_httpd = None
_stopping = threading.Event()
_vnc_port = None
_ws_port = None
_http_port = None
_vnc_lock = threading.Lock()
_vnc_restarting = False
_current_screen = "all"


class _AdoptedProcess:
    """Wrap a PID for a child process we inherited across execv."""

    def __init__(self, pid, name):
        self.pid = pid
        self.args = [name]
        self.returncode = None

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        try:
            os.kill(self.pid, 0)
            return None
        except ProcessLookupError:
            self.returncode = -1
            return self.returncode

    def terminate(self):
        os.kill(self.pid, signal.SIGTERM)

    def kill(self):
        os.kill(self.pid, signal.SIGKILL)

    def wait(self, timeout=None):
        deadline = time.time() + (timeout or 60)
        while time.time() < deadline:
            try:
                pid, status = os.waitpid(self.pid, os.WNOHANG)
                if pid:
                    self.returncode = os.waitstatus_to_exitcode(status)
                    return self.returncode
            except ChildProcessError:
                if self.poll() is not None:
                    return self.returncode
            time.sleep(0.05)
        raise subprocess.TimeoutExpired(self.args, timeout)


def _parse_screens():
    try:
        out = subprocess.check_output(["xrandr"], text=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return []
    screens = []
    for m in re.finditer(
        r"^(\S+)\s+connected\s+(?:primary\s+)?(\d+)x(\d+)\+(\d+)\+(\d+)",
        out, re.MULTILINE,
    ):
        screens.append({
            "name": m.group(1),
            "w": int(m.group(2)), "h": int(m.group(3)),
            "x": int(m.group(4)), "y": int(m.group(5)),
        })
    return screens


def _restart_x11vnc(clip=None):
    global _current_screen, _vnc_restarting
    with _vnc_lock:
        _vnc_restarting = True
        for i, p in enumerate(_procs):
            try:
                if p.args[0] == "x11vnc":
                    p.kill()
                    p.wait(timeout=3)
                    break
            except (OSError, subprocess.TimeoutExpired, IndexError):
                if hasattr(p, 'args') and p.args and p.args[0] == "x11vnc":
                    break
        else:
            _vnc_restarting = False
            return False

        cmd = [
            "x11vnc", "-display", DISPLAY, "-viewonly", "-shared",
            "-forever", "-nopw", "-rfbport", str(_vnc_port), "-q",
        ]
        if clip:
            cmd += ["-clip", clip]
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            _vnc_restarting = False
            return False
        _procs[i] = proc
        time.sleep(0.5)
        _vnc_restarting = False
        if proc.poll() is not None:
            return False
        _current_screen = clip or "all"
        return True


HTML = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>spicetab</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:100%%;height:100%%;overflow:hidden;background:#222}
#toolbar{display:flex;align-items:center;gap:6px;padding:4px 8px;background:#1a1a1a;
  border-bottom:1px solid #333;font:13px/1 system-ui,sans-serif;color:#aaa;
  position:relative;z-index:10}
#toolbar button{background:#333;color:#ccc;border:1px solid #444;border-radius:4px;
  padding:4px 10px;cursor:pointer;font:inherit}
#toolbar button:hover{background:#444;color:#fff}
#toolbar button.active{background:#575;border-color:#6a6;color:#fff}
#toolbar .sep{width:1px;height:18px;background:#444;margin:0 4px}
#screen{width:100%%;height:calc(100%% - 31px)}
#msg{color:#888;font:15px/1.4 system-ui,sans-serif;text-align:center;padding-top:45vh}
</style>
</head>
<body>
<div id="toolbar">
  <span>Screen:</span>
  <div id="screen-buttons"></div>
  <div class="sep"></div>
  <button onclick="playerctl('play')" title="Play">&#x25B6;&#xFE0E;</button>
  <button onclick="playerctl('pause')" title="Pause">&#x23F8;&#xFE0E;</button>
  <button onclick="playerctl('play-pause')" title="Play/Pause">&#x23EF;&#xFE0E;</button>
  <div class="sep"></div>
  <button onclick="playerctl('previous')" title="Previous">&#x23EE;&#xFE0E;</button>
  <button onclick="playerctl('next')" title="Next">&#x23ED;&#xFE0E;</button>
</div>
<div id="screen"><p id="msg">Connecting…</p></div>
<script type="module">
import RFB from './novnc/core/rfb.js';

document.getElementById('msg')?.remove();
let rfb;
function connect() {
  rfb = new RFB(document.getElementById('screen'),
                'ws://' + location.hostname + ':%d');
  rfb.scaleViewport = true;
  rfb.resizeSession = false;
  rfb.viewOnly = true;
  rfb.background = '#222';
  rfb.addEventListener('disconnect', e => {
    document.getElementById('screen').innerHTML =
      '<p id="msg">' + (e.detail.clean ? 'Disconnected.' : 'Connection lost.') + '</p>';
  });
}
connect();

// screen switcher
async function loadScreens() {
  const resp = await fetch('/api/screens');
  const data = await resp.json();
  const container = document.getElementById('screen-buttons');
  container.innerHTML = '';
  const allBtn = document.createElement('button');
  allBtn.textContent = 'All';
  allBtn.dataset.screen = 'all';
  if (data.current === 'all') allBtn.classList.add('active');
  allBtn.onclick = () => switchScreen('all');
  container.appendChild(allBtn);
  data.screens.forEach(s => {
    const btn = document.createElement('button');
    btn.textContent = s.name;
    btn.dataset.screen = s.name;
    if (data.current === s.clip) btn.classList.add('active');
    btn.onclick = () => switchScreen(s.name);
    container.appendChild(btn);
  });
}

async function switchScreen(name) {
  const resp = await fetch('/api/screen/' + encodeURIComponent(name), {method: 'POST'});
  if (!resp.ok) return;
  document.querySelectorAll('#screen-buttons button').forEach(b => b.classList.remove('active'));
  document.querySelector('#screen-buttons button[data-screen="' + name + '"]')?.classList.add('active');
  // reconnect after x11vnc restart
  document.getElementById('screen').innerHTML = '<p id="msg">Reconnecting…</p>';
  setTimeout(() => {
    document.getElementById('screen').innerHTML = '';
    connect();
  }, 800);
}
loadScreens();

// media controls
window.playerctl = async function(action) {
  await fetch('/api/playerctl/' + action, {method: 'POST'});
};

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

            if path == "/api/screens":
                screens = _parse_screens()
                data = {
                    "screens": [
                        {
                            "name": s["name"],
                            "clip": f"{s['w']}x{s['h']}+{s['x']}+{s['y']}",
                        }
                        for s in screens
                    ],
                    "current": _current_screen,
                }
                self._send(
                    json.dumps(data).encode(), "application/json",
                )
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

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path

            if path.startswith("/api/screen/"):
                name = urllib.parse.unquote(path[len("/api/screen/"):])
                if name == "all":
                    ok = _restart_x11vnc()
                else:
                    screens = _parse_screens()
                    match = next(
                        (s for s in screens if s["name"] == name), None,
                    )
                    if not match:
                        self.send_error(404, "Unknown screen")
                        return
                    clip = f"{match['w']}x{match['h']}+{match['x']}+{match['y']}"
                    ok = _restart_x11vnc(clip)
                if ok:
                    self.send_response(204)
                    self.end_headers()
                else:
                    self.send_error(500, "Failed to restart x11vnc")
                return

            if path.startswith("/api/playerctl/"):
                action = urllib.parse.unquote(
                    path[len("/api/playerctl/"):]
                )
                if action not in ("play", "pause", "play-pause",
                                  "next", "previous"):
                    self.send_error(400, "Invalid action")
                    return
                try:
                    subprocess.run(
                        ["playerctl", action],
                        timeout=5,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except (FileNotFoundError, subprocess.TimeoutExpired):
                    pass
                self.send_response(204)
                self.end_headers()
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


def _reload():
    """Stash child state in env vars and re-exec ourselves."""
    vnc_proc = next((p for p in _procs if p.args[0] == "x11vnc"), None)
    ws_proc = next((p for p in _procs if p.args[0] != "x11vnc"), None)
    if vnc_proc:
        os.environ[f"{_ENV_PREFIX}VNC_PID"] = str(vnc_proc.pid)
    if ws_proc:
        os.environ[f"{_ENV_PREFIX}WS_PID"] = str(ws_proc.pid)
    os.environ[f"{_ENV_PREFIX}VNC_PORT"] = str(_vnc_port)
    os.environ[f"{_ENV_PREFIX}WS_PORT"] = str(_ws_port)
    os.environ[f"{_ENV_PREFIX}HTTP_PORT"] = str(_http_port)
    os.environ[f"{_ENV_PREFIX}SCREEN"] = _current_screen
    if _httpd:
        _httpd.shutdown()
    print("\n  Reloading...\n")
    os.execv(sys.executable, [sys.executable, str(_SCRIPT)])


def _file_watcher():
    mtime = _SCRIPT.stat().st_mtime
    while not _stopping.wait(1):
        try:
            new_mtime = _SCRIPT.stat().st_mtime
        except OSError:
            continue
        if new_mtime != mtime:
            _reload()
            return


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
    global _httpd, _vnc_port, _ws_port, _http_port, _current_screen

    _ensure_novnc()

    inherited = os.environ.pop(f"{_ENV_PREFIX}VNC_PID", None)

    if inherited:
        # Adopt existing child processes after execv reload
        vnc_port = _vnc_port = int(os.environ.pop(f"{_ENV_PREFIX}VNC_PORT"))
        ws_port = _ws_port = int(os.environ.pop(f"{_ENV_PREFIX}WS_PORT"))
        http_port = _http_port = int(os.environ.pop(f"{_ENV_PREFIX}HTTP_PORT"))
        _current_screen = os.environ.pop(f"{_ENV_PREFIX}SCREEN", "all")
        ws_pid = int(os.environ.pop(f"{_ENV_PREFIX}WS_PID"))
        _procs.append(_AdoptedProcess(int(inherited), "x11vnc"))
        _procs.append(_AdoptedProcess(ws_pid, "websockify"))
    else:
        vnc_port = _vnc_port = _free_port()
        ws_port = _ws_port = _free_port()
        http_port = _http_port = _free_port()

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
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    _httpd = http.server.ThreadingHTTPServer(
        ("127.0.0.1", http_port), _handler_class(ws_port)
    )
    threading.Thread(target=_httpd.serve_forever, daemon=True).start()
    threading.Thread(target=_watchdog, daemon=True).start()
    threading.Thread(target=_file_watcher, daemon=True).start()

    url = f"http://localhost:{http_port}"
    print(f"\n  {url}\n")
    print("Ctrl+C to stop  ·  auto-exits after 5 min idle\n")
    if not inherited:
        webbrowser.open(url)

    try:
        while not _stopping.is_set():
            if not _vnc_restarting:
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
