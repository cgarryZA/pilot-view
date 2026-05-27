"""Garage lights state + control surface.

For now everything is simulated: `trigger_on` / `trigger_off` / `trigger_toggle`
just flip an in-memory flag. When the Shelly is installed we'll swap the body
of those methods to fire local HTTP calls (Shelly Gen2/Gen4 expose a /rpc
endpoint on the LAN). Status will then come from the Shelly's reported state
either via polling or the long-lived connection.
"""

import threading
from datetime import datetime, timezone
from typing import Callable

LightsListener = Callable[[bool, bool], None]  # (old_on, new_on) -> None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Lights:
    def __init__(self) -> None:
        self._on = False
        self._last_changed = _now()
        self._listeners: list[LightsListener] = []
        self._lock = threading.Lock()

    def status_dict(self) -> dict:
        with self._lock:
            return {
                "on": self._on,
                "last_changed": self._last_changed,
                "controllable": True,
            }

    def add_listener(self, fn: LightsListener) -> None:
        self._listeners.append(fn)

    def trigger_on(self) -> dict:
        return self._set_state(True)

    def trigger_off(self) -> dict:
        return self._set_state(False)

    def trigger_toggle(self) -> dict:
        with self._lock:
            current = self._on
        return self._set_state(not current)

    def _set_state(self, on: bool) -> dict:
        with self._lock:
            if self._on == on:
                return self._snapshot_unlocked()
            old = self._on
            self._on = on
            self._last_changed = _now()
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(old, on)
            except Exception as exc:  # pragma: no cover
                print(f"[lights] listener error: {exc}")
        return self.status_dict()

    def _snapshot_unlocked(self) -> dict:
        return {
            "on": self._on,
            "last_changed": self._last_changed,
            "controllable": True,
        }


lights = Lights()
