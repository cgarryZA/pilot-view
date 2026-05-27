import math
import time
from datetime import datetime, timezone

from app.sources.base import CameraSource


GARAGE = {
    "width": 3.0,   # along X (left/right)
    "length": 5.8,  # along Z (entrance to back wall)
    "height": 2.3,  # along Y (floor to ceiling)
}

CAR = {
    "length": 4.30,
    "width": 1.90,
    "height": 1.16,
}

WARN_THRESHOLD = 0.50  # metres
DANGER_THRESHOLD = 0.20


def _classify(clearances: dict[str, float]) -> str:
    smallest = min(clearances.values())
    if smallest < DANGER_THRESHOLD:
        return "danger"
    if smallest < WARN_THRESHOLD:
        return "warning"
    return "safe"


class SyntheticSource(CameraSource):
    name = "synthetic"

    def __init__(self) -> None:
        self._t0 = time.monotonic()

    def state(self) -> dict:
        t = time.monotonic() - self._t0

        # Animate car drift toward the back wall on a slow sine wave.
        # Z = 0 is the entrance plane, Z grows into the garage.
        # Car centre oscillates between a safe distance and almost-touching.
        z_safe = CAR["length"] / 2 + 1.2          # comfortably inside
        z_close = GARAGE["length"] - CAR["length"] / 2 - 0.08  # almost touching
        z_mid = (z_safe + z_close) / 2
        z_amp = (z_close - z_safe) / 2
        car_z = z_mid + z_amp * math.sin(t * 2 * math.pi / 18)  # 18s period

        # Small lateral wobble so we get side-clearance variation too.
        car_x = 0.18 * math.sin(t * 2 * math.pi / 11)

        car_y = CAR["height"] / 2  # car sits on the floor (centroid at half-height)
        car_yaw = 0.06 * math.sin(t * 2 * math.pi / 25)  # tiny yaw drift

        clearances = self._clearances(car_x, car_z)
        state_label = _classify(clearances)

        return {
            "source": self.name,
            "ts": datetime.now(timezone.utc).isoformat(),
            "live_url": "/static/assets/synthetic/garage.png",
            "camera": {
                "connected": True,
                "model": "Orbbec Gemini 2 (synthetic)",
                "interface": "Synthetic loopback",
                "serial": "SYN-0001",
                "firmware": "synthetic-1.0",
                "last_attempt": datetime.now(timezone.utc).isoformat(),
                "error": None,
            },
            "geometry": {
                "garage": {
                    "width": GARAGE["width"],
                    "length": GARAGE["length"],
                    "height": GARAGE["height"],
                },
                "car": {
                    "position": {"x": car_x, "y": car_y, "z": car_z},
                    "yaw": car_yaw,
                    "extent": {
                        "length": CAR["length"],
                        "width": CAR["width"],
                        "height": CAR["height"],
                    },
                    "model": "lamborghini_gallardo",
                },
                "clearances": clearances,
                "thresholds": {
                    "warn": WARN_THRESHOLD,
                    "danger": DANGER_THRESHOLD,
                },
                "state": state_label,
            },
        }

    def _clearances(self, car_x: float, car_z: float) -> dict[str, float]:
        half_l = CAR["length"] / 2
        half_w = CAR["width"] / 2
        return {
            "front": GARAGE["length"] - (car_z + half_l),
            "rear": car_z - half_l,
            "left": (GARAGE["width"] / 2) + car_x - half_w,
            "right": (GARAGE["width"] / 2) - car_x - half_w,
            "ceiling": GARAGE["height"] - CAR["height"],
        }
