import math
import time
from datetime import datetime, timezone

from app import calibration
from app.sources.base import CameraSource


def _classify(clearances: dict[str, float], thresholds: dict[str, float]) -> str:
    smallest = min(clearances.values())
    if smallest < thresholds["danger"]:
        return "danger"
    if smallest < thresholds["warn"]:
        return "warning"
    return "safe"


class SyntheticSource(CameraSource):
    name = "synthetic"

    def __init__(self) -> None:
        self._t0 = time.monotonic()

    def state(self) -> dict:
        cal = calibration.load()
        garage = cal["garage"]
        car = cal["vehicle"]["extent"]
        thresholds = cal["thresholds"]

        t = time.monotonic() - self._t0

        z_safe = car["length"] / 2 + 1.2
        z_close = max(z_safe + 0.4, garage["length"] - car["length"] / 2 - 0.08)
        z_mid = (z_safe + z_close) / 2
        z_amp = (z_close - z_safe) / 2
        car_z = z_mid + z_amp * math.sin(t * 2 * math.pi / 18)

        max_lateral = max(0.0, (garage["width"] / 2 - car["width"] / 2) * 0.6)
        car_x = max_lateral * math.sin(t * 2 * math.pi / 11)

        car_y = car["height"] / 2
        car_yaw = 0.06 * math.sin(t * 2 * math.pi / 25)

        clearances = self._clearances(car_x, car_z, garage, car)
        state_label = _classify(clearances, thresholds)

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
                    "width": garage["width"],
                    "length": garage["length"],
                    "height": garage["height"],
                },
                "car": {
                    "position": {"x": car_x, "y": car_y, "z": car_z},
                    "yaw": car_yaw,
                    "extent": {
                        "length": car["length"],
                        "width": car["width"],
                        "height": car["height"],
                    },
                    "model": "lamborghini_gallardo",
                },
                "clearances": clearances,
                "thresholds": {
                    "warn": thresholds["warn"],
                    "danger": thresholds["danger"],
                },
                "state": state_label,
            },
        }

    def _clearances(self, car_x, car_z, garage, car) -> dict[str, float]:
        half_l = car["length"] / 2
        half_w = car["width"] / 2
        return {
            "front": garage["length"] - (car_z + half_l),
            "rear": car_z - half_l,
            "left": (garage["width"] / 2) + car_x - half_w,
            "right": (garage["width"] / 2) - car_x - half_w,
            "ceiling": garage["height"] - car["height"],
        }
