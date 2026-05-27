"""Cross-device automations.

Keep these in one place so the rules are easy to find and reason about. Each
automation is just a listener/guard on one device that calls another. Modules
wire themselves up at import time via `install()`.
"""

from typing import Optional

from app.door import CLOSED, door
from app.lights import lights
from app.sources import source

# Minimum gap between the rear of the car bounding box and the entrance plane
# before we'll allow the door to operate. A small buffer protects against
# detection noise that briefly reports a positive clearance when the box is
# actually flush with the doorway.
ENTRANCE_SAFETY_BUFFER_M = 0.02  # 2 cm


def install() -> None:
    # ── Lights come on whenever the door leaves the fully-closed state ──
    # Triggered by the 'closed' sensor going inactive — so it fires regardless
    # of whether the door was opened via the web, the fob, or by hand.
    def on_door_change(old_status: str, new_status: str) -> None:
        if old_status == CLOSED and new_status != CLOSED:
            lights.trigger_on()

    door.add_listener(on_door_change)

    # ── Safety guard: block the door if the car's bounding box clips the
    # entrance plane. A roller door moving while the car is in the doorway is
    # how bodywork gets damaged. ──
    def guard_vehicle_clipping_entrance() -> Optional[str]:
        try:
            state = source.state()
        except Exception:
            return None  # don't block if we can't read state
        geom = state.get("geometry")
        if not geom:
            return None  # camera disconnected → no signal, allow
        clearances = geom.get("clearances") or {}
        rear = clearances.get("rear")
        if rear is None:
            return None
        if rear < ENTRANCE_SAFETY_BUFFER_M:
            return (
                f"Vehicle bounding box clipping the entrance "
                f"(rear clearance {rear * 100:.0f} cm) — door blocked for safety"
            )
        return None

    door.add_guard(guard_vehicle_clipping_entrance)
