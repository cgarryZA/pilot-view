"""Web terminal — a real PTY-backed shell bridged over a WebSocket.

This exists because once the Pi becomes its own Wi-Fi access point there is no
other practical way to administer it: a PC that isn't on the AP can't SSH in, so
you drive the box from a phone browser instead. The self-contained /terminal
page (locally-vendored xterm.js) means this works even with zero internet.

Protocol (client → server): JSON text frames
    {"type": "input",  "data": "<keystrokes>"}
    {"type": "resize", "cols": N, "rows": M}
Server → client: raw terminal output as binary frames.

Security — this is a root-capable shell, so it is gated independently of the
dashboard:
  • OPT-IN: the feature is OFF unless PILOT_VIEW_TERMINAL=enabled.
  • A valid session cookie always authorises (normal logged-in use).
  • Otherwise a token is required as a ?token= query param. It comes from
    PILOT_VIEW_TERMINAL_TOKEN (set by the AP setup); if the terminal is enabled
    with no token configured we mint a random one at startup and log it.
  • We deliberately do NOT honour PILOT_VIEW_AUTH=disabled here. Disabling the
    app login (so a phone can use the dashboard on the AP without a passkey) must
    never, on its own, hand a stranger on the Wi-Fi a shell.
"""

import asyncio
import json
import os
import secrets
import signal
import struct
from typing import Optional

# pty/fcntl/termios are Unix-only. Guard them so the app can still run on a
# Windows dev box (the web terminal simply disables itself there).
try:
    import fcntl
    import pty
    import termios
    _PTY_AVAILABLE = True
except ImportError:  # pragma: no cover — non-Unix dev machine
    _PTY_AVAILABLE = False

from fastapi import WebSocket, WebSocketDisconnect

from app.passkeys import session_from_cookies

SHELL = "/bin/bash"

# A process-lifetime token, minted only if the terminal is enabled without an
# explicit PILOT_VIEW_TERMINAL_TOKEN. Logged once so an admin can recover it.
_RUNTIME_TOKEN: Optional[str] = None


def terminal_enabled() -> bool:
    if not _PTY_AVAILABLE:
        return False
    # Opt-in: the shell is off unless explicitly enabled.
    return os.getenv("PILOT_VIEW_TERMINAL", "disabled").strip().lower() == "enabled"


def _configured_token() -> Optional[str]:
    tok = os.getenv("PILOT_VIEW_TERMINAL_TOKEN", "").strip()
    return tok or None


def _effective_token() -> Optional[str]:
    """The token a client must present: the configured one, or a random one
    minted (and logged) the first time it's needed."""
    global _RUNTIME_TOKEN
    tok = _configured_token()
    if tok:
        return tok
    if _RUNTIME_TOKEN is None:
        _RUNTIME_TOKEN = secrets.token_urlsafe(18)
        print(
            "[terminal] PILOT_VIEW_TERMINAL_TOKEN is not set; generated a one-time "
            f"token for this run: {_RUNTIME_TOKEN}",
            flush=True,
        )
        print(f"[terminal]   connect via  /terminal?token={_RUNTIME_TOKEN}", flush=True)
    return _RUNTIME_TOKEN


def _has_valid_session(socket: WebSocket) -> bool:
    return session_from_cookies(socket.cookies) is not None


def _authorized(socket: WebSocket) -> bool:
    # A genuine authenticated session always works.
    if _has_valid_session(socket):
        return True
    # Otherwise require the terminal token. Constant-time compare. Crucially this
    # path is reachable even when PILOT_VIEW_AUTH=disabled — that flag relaxes the
    # dashboard, never the shell.
    presented = socket.query_params.get("token") if socket.query_params else None
    expected = _effective_token()
    return bool(presented and expected and secrets.compare_digest(presented, expected))


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
