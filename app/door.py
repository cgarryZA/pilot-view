"""Garage door state + control surface.

The Door class models a garage door using **two reed-switch sensors** — one at the
fully-closed position and one at the fully-open position. Status is derived from
the pair:

    closed sensor   open sensor    status
    ─────────────   ───────────    ─────────
    active          inactive       closed
    inactive        active         open
    inactive        inactive       partial   (moving or stuck mid-travel)
    active          active         fault     (sensor or wiring issue)

For now everything is simulated: `trigger_open` / `trigger_close` flip the sensors
on a timer. When real hardware arrives:
  * Two GPIO inputs will be wired to the reed switches and update `_sensors`
    via interrupt callbacks (replacing the timer-driven sim).
  * A GPIO output to a relay (across the opener's wall-button terminals) will
    pulse inside `trigger_open` / `trigger_close` to actually move the door.

State transitions notify any registered listeners — used to chain automations
(e.g. lights on when the door is no longer fully closed).
"""

import threading
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

TRANSITION_SECONDS = 4.0

# Status names — derived from the sensor pair.
CLOSED = "closed"
OPEN = "open"
PARTIAL = "partial"
FAULT = "fault"

DoorListener = Callable[[str, str], None]  # (old_status, new_status) -> None

# A guard returns None to allow the trigger, or a human-readable reason to block.
DoorGuard = Callable[[], Optional[str]]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Door:
    def __init__(self) -> None:
        # Default: fully closed (closed sensor active, open sensor inactive)
        self._sensor_closed = True
        self._sensor_open = False
        self._last_changed = _now()
        self._timer: threading.Timer | None = None
        self._listeners: list[DoorListener] = []
        self._guards: list[DoorGuard] = []
        self._lock = threading.Lock()

    # ── status derivation ───────────────────────────────────────────────

    def status(self) -> str:
        return self._derive_status(self._sensor_closed, self._sensor_open)

    @staticmethod
    def _derive_status(sc: bool, so: bool) -> str:
        if sc and not so:
            return CLOSED
        if so and not sc:
            return OPEN
        if not sc and not so:
            return PARTIAL
        return FAULT

    def status_dict(self) -> dict:
        with self._lock:
            return {
                "status": self.status(),
                "sensors": {"closed": self._sensor_closed, "open": self._sensor_open},
                "last_changed": self._last_changed,
                "controllable": True,
            }

    # ── listeners ───────────────────────────────────────────────────────

    def add_listener(self, fn: DoorListener) -> None:
        self._listeners.append(fn)

    def add_guard(self, fn: DoorGuard) -> None:
        """Register a callback that can veto an open/close trigger.

        Called BEFORE any state change. Return None to allow, or a string
        describing why the trigger should be blocked. Guards run outside the
        Door's lock so they're free to inspect other modules (camera state, etc).
        """
        self._guards.append(fn)

    def _check_guards(self) -> Optional[str]:
        for guard in list(self._guards):
            try:
                reason = guard()
            except Exception as exc:  # pragma: no cover — defensive
                print(f"[door] guard error: {exc}")
                continue
            if reason:
                return reason
        return None

    # ── triggers (simulated for now) ────────────────────────────────────

    def trigger_open(self) -> dict:
        blocked = self._check_guards()
        if blocked:
            snap = self.status_dict()
            snap["blocked_reason"] = blocked
            return snap
        return self._begin_transition(
            allowed_from=(CLOSED, PARTIAL),
            new_closed=False,
            new_open=False,
            final_closed=False,
            final_open=True,
        )

    def trigger_close(self) -> dict:
        blocked = self._check_guards()
        if blocked:
            snap = self.status_dict()
            snap["blocked_reason"] = blocked
            return snap
        return self._begin_transition(
            allowed_from=(OPEN, PARTIAL),
            new_closed=False,
            new_open=False,
            final_closed=True,
            final_open=False,
        )

    def trigger_toggle(self) -> dict:
        blocked = self._check_guards()
        if blocked:
            snap = self.status_dict()
            snap["blocked_reason"] = blocked
            return snap
        with self._lock:
            current = self.status()
        if current in (CLOSED, PARTIAL):
            return self.trigger_open()
        if current == OPEN:
            return self.trigger_close()
        # FAULT — refuse to act blindly
        return self.status_dict()

    # ── internals ───────────────────────────────────────────────────────

    def _begin_transition(
        self,
        allowed_from: Iterable[str],
        new_closed: bool,
        new_open: bool,
        final_closed: bool,
        final_open: bool,
    ) -> dict:
        with self._lock:
            if self.status() not in allowed_from:
                return self._snapshot_unlocked()

            # Step into the "moving" phase: both sensors inactive.
            self._update_sensors_unlocked(new_closed, new_open)

            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(
                TRANSITION_SECONDS,
                self._finish_transition,
                args=(final_closed, final_open),
            )
            self._timer.daemon = True
            self._timer.start()
            return self._snapshot_unlocked()

    def _finish_transition(self, final_closed: bool, final_open: bool) -> None:
        with self._lock:
            self._update_sensors_unlocked(final_closed, final_open)
            self._timer = None

    def _update_sensors_unlocked(self, sc: bool, so: bool) -> None:
        """Set both sensors; emit listener events if status changed.

        Called while self._lock is held. Listeners are invoked OUTSIDE the lock
        to avoid deadlocking if a listener calls back into Door.
        """
        old_status = self.status()
        self._sensor_closed = sc
        self._sensor_open = so
        new_status = self.status()
        if new_status != old_status:
            self._last_changed = _now()
            # Snapshot listeners list to avoid mutation during iteration.
            listeners = list(self._listeners)
            self._lock.release()
            try:
                for fn in listeners:
                    try:
                        fn(old_status, new_status)
                    except Exception as exc:  # pragma: no cover
                        print(f"[door] listener error: {exc}")
            finally:
                self._lock.acquire()

    def _snapshot_unlocked(self) -> dict:
        return {
            "status": self.status(),
            "sensors": {"closed": self._sensor_closed, "open": self._sensor_open},
            "last_changed": self._last_changed,
            "controllable": True,
        }


door = Door()
