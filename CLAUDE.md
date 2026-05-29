# spicetab

Read-only desktop viewer — serves the current X desktop in a browser via x11vnc + noVNC.

## Run

```
make
```

Runs `uv run python serve.py` which handles venv, deps, noVNC download, and launches everything.

## Architecture

Three processes managed by `serve.py`:

1. **x11vnc** — VNC server on the current `$DISPLAY` (default `:0`), read-only, no password
2. **websockify** — WebSocket-to-TCP proxy bridging the browser to x11vnc
3. **Python HTTP server** — serves the viewer page + noVNC static files + `/ping` heartbeat

All use random free ports. The browser connects to the HTTP server, loads noVNC JS, and opens a WebSocket to websockify.

## Auto-exit

The browser page sends `/ping` every 15s. If no ping arrives for 5 minutes, the script kills everything and exits. Ctrl+C also does a clean shutdown.

## Dependencies

- **System:** `x11vnc` (apt install x11vnc)
- **Python (via uv):** `websockify`
- **Browser (auto-downloaded):** noVNC v1.4.0 (cached in `.novnc/`, gitignored)

## Key decisions

- noVNC over spice-html5: spice-html5 is for the SPICE protocol (KVM/QEMU VMs), x11vnc speaks VNC
- Two-server design (HTTP + websockify) rather than one: lets us add the `/ping` heartbeat endpoint for auto-exit without patching websockify
- noVNC's `scaleViewport = true` handles fit-to-window + resize tracking automatically
