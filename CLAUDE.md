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

## Screen switching

The toolbar has buttons for "All" plus each connected monitor (parsed from `xrandr`). Clicking one restarts x11vnc with `-clip WxH+X+Y` for that monitor, or without `-clip` for all. The browser reconnects automatically after the restart.

## Window picker

Next to the screen picker, a dropdown button lists individual windows (via `wmctrl -lG`). Only windows on currently visible virtual desktops are shown — visible desktops are detected by grouping `wmctrl -d` viewport positions (one desktop per unique viewport = one per monitor, supporting per-monitor workspaces like i3). When a specific monitor is selected, the list is further filtered to windows overlapping that monitor's geometry.

Selecting a window restarts x11vnc with `-clip WxH+X+Y` using the window's frame geometry from `xwininfo -id <wid> -frame`. The window ID is validated server-side (`^0x[0-9a-fA-F]+$`).

## Media controls

Toolbar buttons for play, pause, play-pause (grouped), then previous/next (separated). All call `playerctl <action>` on the server via `POST /api/playerctl/<action>`.

## Auto-reload

A file watcher thread polls `serve.py`'s mtime every second. On change, it stashes child PIDs and ports into env vars and does `os.execv` to re-exec. The new process adopts the existing x11vnc/websockify via `_AdoptedProcess` (a Popen-like wrapper around a PID), rebinds the HTTP server on the same port (`SO_REUSEADDR`), and skips reopening the browser. x11vnc and websockify keep running undisturbed.

## Stale process cleanup

On start, kills any x11vnc processes not belonging to the current session (skips the inherited PID on reload). Sends a `notify-send` if any were killed.

## Connection notifications

Tracks client IPs via `/ping`. The first client is silently accepted (the browser we opened). Any new IP after that triggers `notify-send "New connection from <ip>"`.

## Dependencies

- **System:** `x11vnc`, `playerctl`, `xrandr`, `wmctrl`, `xwininfo`, `notify-send` (libnotify)
- **Python (via uv):** `websockify`
- **Browser (auto-downloaded):** noVNC v1.4.0 (cached in `.novnc/`, gitignored)

## Key decisions

- noVNC over spice-html5: spice-html5 is for the SPICE protocol (KVM/QEMU VMs), x11vnc speaks VNC
- Two-server design (HTTP + websockify) rather than one: lets us add the `/ping` heartbeat endpoint for auto-exit without patching websockify
- noVNC's `scaleViewport = true` handles fit-to-window + resize tracking automatically
- x11vnc restart uses SIGKILL not SIGTERM — x11vnc doesn't handle SIGTERM cleanly
- Auto-reload uses `os.execv` with env var handoff rather than an external watcher tool — keeps it self-contained with no extra deps
