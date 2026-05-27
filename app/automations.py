"""Cross-device automations.

Keep these in one place so the rules are easy to find and reason about. Each
automation is just a listener on one device that calls another. Modules wire
themselves up at import time via `install()`.
"""

from app.door import CLOSED, door
from app.lights import lights


def install() -> None:
    # Lights come on whenever the door leaves the fully-closed state.
    # Triggered by the 'closed' sensor going inactive — so it fires regardless
    # of whether the door was opened via the web, the fob, or by hand.
    def on_door_change(old_status: str, new_status: str) -> None:
        if old_status == CLOSED and new_status != CLOSED:
            lights.trigger_on()

    door.add_listener(on_door_change)
