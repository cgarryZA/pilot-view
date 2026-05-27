"""Per-vehicle 12 V battery monitor.

Each vehicle in the calibration registry may have a `battery_monitor_mac` field.
If set and a working driver is available, we read voltage (and optionally
charging state) for that vehicle. The synthetic driver simulates plausible
12 V lead-acid behaviour including periodic charging cycles, so the UI
"charging" animation is testable without hardware.

Real driver target: BM2 (BLE GATT, reverse-engineered protocol). Stubbed for
now — actual implementation will plug into the same Driver interface.
"""

import math
import os
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BatteryDriver(ABC):
    name: str = "base"

    @abstractmethod
    def read(self) -> dict:
        """Return:
        {
          "available": bool,
          "voltage_v": float | None,
          "charging": bool | None,
          "last_read": iso8601 | None,
        }
        """


class SyntheticBatteryDriver(BatteryDriver):
    """Plausible 12V behaviour with a periodic charging cycle.

    Cycle length is randomized per-instance so multiple vehicles don't charge
    in lockstep.
    """
    name = "synthetic"

    def __init__(self, seed: int = 0) -> None:
        self._t0 = time.monotonic()
        self._cycle = 240 + (seed % 7) * 30   # 4-7 min cycle
        self._charge_for = 60 + (seed % 5) * 10  # 60-100s charging
        self._base = 12.55 + (seed % 3) * 0.05   # slight per-vehicle variation

    def read(self) -> dict:
        t = time.monotonic() - self._t0
        within = t % self._cycle
        if within < self._charge_for:
            # Charging ramp-up to 13.6V then plateau
            ramp = min(1.0, within / 6.0)
            voltage = self._base + (13.6 - self._base) * ramp + 0.05 * math.sin(within / 5)
            charging = True
        else:
            # Idle drift around base
            voltage = self._base + 0.08 * math.sin(t / 90)
            charging = False
        return {
            "available": True,
            "voltage_v": round(voltage, 2),
            "charging": charging,
            "last_read": _now(),
        }


class UnavailableBatteryDriver(BatteryDriver):
    name = "unavailable"

    def read(self) -> dict:
        return {
            "available": False,
            "voltage_v": None,
            "charging": None,
            "last_read": None,
        }


class BM2Driver(BatteryDriver):
    """BM2 BLE driver — stub. Reads voltage from a specific BM2 device by MAC.

    Real implementation will:
      - Open a BLE connection (bleak) to the BM2 at `mac`
      - Subscribe to its notification characteristic
      - Parse the proprietary advertisement payload to extract voltage
      - Detect "charging" by voltage > calibration.battery.charging_v

    For now this fails fast — the manager catches the exception and falls back
    to UnavailableBatteryDriver, so the UI shows "battery unavailable" cleanly.
    """
    name = "bm2"

    def __init__(self, mac: str) -> None:
        self._mac = mac
        raise NotImplementedError(
            "BM2Driver not implemented yet — install bm2-meter and wire when device arrives"
        )

    def read(self) -> dict:  # pragma: no cover
        raise NotImplementedError


def _backend() -> str:
    return os.getenv("PILOT_VIEW_BATTERY", "synthetic").strip().lower()


class BatteryMonitor:
    """Manager that holds a per-vehicle driver instance.

    Cached by vehicle id so the synthetic state machine has continuity across
    polls, and the BM2 BLE connection is opened once per device.
    """
    def __init__(self) -> None:
        self._drivers: dict[str, BatteryDriver] = {}

    def read(self, vehicle_id: str, mac: Optional[str]) -> Optional[dict]:
        if not mac:
            return None
        key = vehicle_id
        if key not in self._drivers:
            self._drivers[key] = self._make_driver(mac, vehicle_id)
        return self._drivers[key].read()

    def _make_driver(self, mac: str, vehicle_id: str) -> BatteryDriver:
        backend = _backend()
        if backend == "bm2":
            try:
                return BM2Driver(mac)
            except Exception as exc:
                print(f"[battery] BM2 init failed for {vehicle_id} ({mac}): {exc}")
                return UnavailableBatteryDriver()
        # Synthetic: seed from MAC string so each vehicle gets a different cycle.
        seed = sum(ord(c) for c in mac) if mac else 0
        return SyntheticBatteryDriver(seed=seed)


battery_monitor = BatteryMonitor()
