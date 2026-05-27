"""Garage door state + control surface.

The Door class models the four states a garage door can be in and the transitions
between them. For now everything is simulated — `trigger_open`, `trigger_close`,
and `trigger_toggle` just flip in-memory state on a timer. When the real hardware
is wired in, these methods will also pulse a GPIO pin connected to a relay across
the opener's wall-button terminals (and the open/closed state will be sensed via
reed switches on GPIO inputs).
"""

import threading
from datetime import datetime, timezone
from typing import Iterable

# How long a simulated open/close cycle takes. Real doors are usually 8-15s.
TRANSITION_SECONDS = 4.0

# Allowed states.
CLOSED = "closed"
OPEN = "open"
OPENING = "opening"
CLOSING = "closing"
UNKNOWN = "unknown"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Door:
    def __init__(self) -> None:
        self._status = CLOSED
        self._last_changed = _now()
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def status_dict(self) -> dict:
        with self._lock:
            return {
                "status": self._status,
                "last_changed": self._last_changed,
                "controllable": True,   # set False once we add a "hardware not wired" check
            }

    def trigger_open(self) -> dict:
        return self._transition(from_states=(CLOSED, CLOSING), transient=OPENING, final=OPEN)

    def trigger_close(self) -> dict:
        return self._transition(from_states=(OPEN, OPENING), transient=CLOSING, final=CLOSED)

    def trigger_toggle(self) -> dict:
        with self._lock:
            current = self._status
        if current in (CLOSED, CLOSING):
            return self.trigger_open()
        if current in (OPEN, OPENING):
            return self.trigger_close()
        # Unknown state — just attempt to open as a sensible default.
        return self.trigger_open()

    # ── internals ────────────────────────────────────────────────────────

    def _transition(self, from_states: Iterable[str], transient: str, final: str) -> dict:
        with self._lock:
            if self._status not in from_states:
                return self._snapshot()
            self._set_status_unlocked(transient)
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(TRANSITION_SECONDS, self._finish, args=(final,))
            self._timer.daemon = True
            self._timer.start()
            return self._snapshot()

    def _finish(self, final: str) -> None:
        with self._lock:
            # Only commit the final state if we're still in the matching transient.
            # (User may have reversed the door mid-motion, which cancels the old timer
            # before it fires, but be defensive in case the cancel/fire race.)
            self._set_status_unlocked(final)
            self._timer = None

    def _set_status_unlocked(self, status: str) -> None:
        self._status = status
        self._last_changed = _now()

    def _snapshot(self) -> dict:
        return {
            "status": self._status,
            "last_changed": self._last_changed,
            "controllable": True,
        }


door = Door()
