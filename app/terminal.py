"""Web terminal — a real PTY-backed shell bridged over a WebSocket.

This exists because once the Pi becomes its own Wi-Fi access point there is no
other practical way to administer it: a PC that isn't on the AP can't SSH in, so
you drive the box from a phone browser instead. The self-contained /terminal
page (locally-vendored xterm.js) means this works even with zero internet.

Protocol (client → server): JSON text frames
    {"type": "input",  "data": "<keystrokes>"}
    {"type": "resize", "cols": N, "rows": M}
Server → client: raw terminal output as binary frames.

Security: gated exactly like the main /ws. When PILOT_VIEW_AUTH=disabled the
Wi-Fi/WPA2 password is the only gate (the model the AP setup uses); otherwise a
valid session cookie is required. Set PILOT_VIEW_TERMINAL=disabled to turn the
feature off completely.
"""

import asyncio
import fcntl
import json
import os
import pty
import signal
import struct
import termios

from fastapi import WebSocket, WebSocketDisconnect

from app.passkeys import SESSION_COOKIE_NAME, sessions

SHELL = "/bin/bash"


def terminal_enabled() -> bool:
    return os.getenv("PILOT_VIEW_TERMINAL", "enabled").strip().lower() != "disabled"


def _auth_disabled() -> bool:
    return os.getenv("PILOT_VIEW_AUTH", "enabled").strip().lower() == "disabled"


def _authorized(socket: WebSocket) -> bool:
    if _auth_disabled():
        return True
    sid = socket.cookies.get(SESSION_COOKIE_NAME)
    return bool(sid and sessions.get(sid))


async def terminal_endpoint(socket: WebSocket) -> None:
    if not terminal_enabled():
        await socket.close(code=1008)
        return
    if not _authorized(socket):
        await socket.close(code=1008)
        return
    await socket.accept()

    # Build the child environment BEFORE forking so the (fragile) post-fork
    # window does nothing but syscalls.
    child_env = dict(os.environ)
    child_env["TERM"] = "xterm-256color"
    # The systemd unit pins PATH to the venv only; a real shell needs the system
    # dirs or sudo/nmcli/systemctl/bash won't be found. Prepend the standard set.
    system_path = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    child_env["PATH"] = system_path + ":" + child_env.get("PATH", "")
    home = os.path.expanduser("~")
    child_env.setdefault("HOME", home)

    pid, master_fd = pty.fork()
    if pid == 0:
        # ── child ── stdin/stdout/stderr are already wired to the PTY slave.
        try:
            os.chdir(home)
        except Exception:
            pass
        os.execvpe(SHELL, [SHELL, "-il"], child_env)
        os._exit(1)  # only reached if exec failed

    loop = asyncio.get_running_loop()

    def _read_master() -> bytes:
        try:
            return os.read(master_fd, 65536)
        except OSError:
            return b""

    async def pump_to_client() -> None:
        while True:
            data = await loop.run_in_executor(None, _read_master)
            if not data:  # shell exited / PTY closed
                break
            try:
                await socket.send_bytes(data)
            except Exception:
                break
        try:
            await socket.close()
        except Exception:
            pass

    out_task = asyncio.create_task(pump_to_client())
    try:
        while True:
            msg = await socket.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            text = msg.get("text")
            if text is None:
                raw = msg.get("bytes")
                if raw:
                    os.write(master_fd, raw)
                continue
            try:
                obj = json.loads(text)
            except Exception:
                continue
            kind = obj.get("type")
            if kind == "input":
                os.write(master_fd, str(obj.get("data", "")).encode())
            elif kind == "resize":
                try:
                    rows = max(1, int(obj.get("rows", 24)))
                    cols = max(1, int(obj.get("cols", 80)))
                    fcntl.ioctl(
                        master_fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0),
                    )
                except Exception:
                    pass
    except WebSocketDisconnect:
        pass
    finally:
        out_task.cancel()
        try:
            os.close(master_fd)
        except Exception:
            pass
        try:
            os.kill(pid, signal.SIGHUP)
        except Exception:
            pass
        try:
            os.waitpid(pid, os.WNOHANG)
        except Exception:
            pass
