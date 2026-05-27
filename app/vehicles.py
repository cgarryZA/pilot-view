"""Vehicles registry + active-vehicle detection.

Registry of known vehicles lives in calibration. Active vehicle is chosen by a
driver:

  - SyntheticVehicleDriver  : in-memory active id; UI 'set_active' calls flip it.
  - BeaconVehicleDriver     : looks at beacons.snapshot(), picks the registered
                              vehicle with the strongest matching beacon (above
                              an RSSI threshold).

When the real driver is in use, set_active() is a no-op — the source of truth
is the beacon scan.
"""

import os
import threading
from typing import Optional

from app import calibration
from app.beacons import beacons

# Beacons weaker than this are ignored as "too far away to be this car".
MIN_RSSI_DBM = -90


class VehicleDriver:
    name: str = "base"

    def active_id(self, registry: dict) -> Optional[str]:
        raise NotImplementedError

    def set_active(self, vehicle_id: Optional[str]) -> None:
        # Default: ignored (real driver should not be overridable).
        pass


class SyntheticVehicleDriver(VehicleDriver):
    name = "synthetic"

    def __init__(self) -> None:
        self._active_id: Optional[str] = None
        self._lock = threading.Lock()

    def active_id(self, registry: dict) -> Optional[str]:
        with self._lock:
            current = self._active_id
        if current and current in registry:
            return current
        # Default to first vehicle if not set or invalid
        return next(iter(registry.keys())) if registry else None

    def set_active(self, vehicle_id: Optional[str]) -> None:
        with self._lock:
            self._active_id = vehicle_id


class BeaconVehicleDriver(VehicleDriver):
    name = "beacons"

    def active_id(self, registry: dict) -> Optional[str]:
        snapshot = beacons.snapshot()
        if not snapshot:
            return None

        # Find the registered vehicle whose beacon is currently strongest.
        best_id: Optional[str] = None
        best_rssi = MIN_RSSI_DBM
        for vid, vdata in registry.items():
            beacon_id = (vdata.get("beacon_id") or "").upper()
            if not beacon_id:
                continue
            info = snapshot.get(beacon_id)
            if not info:
                continue
            if info["rssi"] > best_rssi:
                best_rssi = info["rssi"]
                best_id = vid
        return best_id


def _make_driver() -> VehicleDriver:
    name = os.getenv("PILOT_VIEW_VEHICLES", "synthetic").strip().lower()
    if name == "beacons":
        return BeaconVehicleDriver()
    return SyntheticVehicleDriver()


class Vehicles:
    def __init__(self) -> None:
        self._driver = _make_driver()

    @property
    def driver_name(self) -> str:
        return self._driver.name

    def state_dict(self) -> dict:
        cal = calibration.load()
        vehicles_cfg = cal.get("vehicles", {})
        registry = vehicles_cfg.get("registry", {})
        active_id = self._driver.active_id(registry)
        active = registry.get(active_id) if active_id else None
        return {
            "driver": self._driver.name,
            "active_id": active_id,
            "active": active,
            # Strip large fields for the snapshot list (UI just needs id + name).
            "list": [
                {"id": vid, "name": v.get("name", vid)}
                for vid, v in registry.items()
            ],
        }

    def set_active(self, vehicle_id: Optional[str]) -> dict:
        self._driver.set_active(vehicle_id)
        return self.state_dict()

    def cycle_active(self) -> dict:
        cal = calibration.load()
        registry = cal.get("vehicles", {}).get("registry", {})
        if not registry:
            return self.state_dict()
        ids = list(registry.keys())
        current = self._driver.active_id(registry)
        if current and current in ids:
            idx = (ids.index(current) + 1) % len(ids)
        else:
            idx = 0
        self._driver.set_active(ids[idx])
        return self.state_dict()


vehicles = Vehicles()
