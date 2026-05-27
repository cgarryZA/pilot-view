"""BLE beacon scanner abstraction.

Scans for BLE advertisements (iBeacon / Eddystone / raw MAC) and returns a map
of beacon_id → RSSI. Other modules consume this to derive things like "which
car is currently in the garage" (see app/vehicles.py).

Real driver uses `bleak` and runs an async scanner in the background. Synthetic
driver returns an empty dict — the synthetic vehicle driver uses its own
in-memory active id instead, so beacon emptiness is fine in dev mode.
"""

import os
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timezone

# How long a beacon advertisement is "fresh" before we drop it.
TTL_SECONDS = 6.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BeaconDriver(ABC):
    name: str = "base"

    @abstractmethod
    def snapshot(self) -> dict:
        """Return current map of beacon_id → {'rssi': int, 'last_seen': iso8601}."""


class SyntheticBeaconDriver(BeaconDriver):
    name = "synthetic"

    def snapshot(self) -> dict:
        # No beacons in synthetic mode — vehicle module uses its in-memory id instead.
        return {}


class BleakBeaconDriver(BeaconDriver):
    """Background BLE scanner using bleak.

    Lazy-imports bleak; if unavailable on the host (e.g. dev laptop without BLE
    libs) we fall back to the synthetic driver in `make_driver()` below.
    """
    name = "bleak"

    def __init__(self) -> None:
        import asyncio
        from bleak import BleakScanner  # type: ignore

        self._asyncio = asyncio
        self._BleakScanner = BleakScanner
        self._lock = threading.Lock()
        self._last_seen: dict[str, tuple[int, float]] = {}  # beacon_id -> (rssi, monotonic)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def snapshot(self) -> dict:
        import time
        cutoff = time.monotonic() - TTL_SECONDS
        out: dict = {}
        with self._lock:
            for bid, (rssi, ts) in list(self._last_seen.items()):
                if ts < cutoff:
                    self._last_seen.pop(bid, None)
                    continue
                out[bid] = {"rssi": rssi, "last_seen": _now()}
        return out

    def _run(self) -> None:
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._scan_forever())
        finally:
            loop.close()

    async def _scan_forever(self) -> None:
        import time

        def on_advert(device, advertisement_data):
            # Identify by device MAC for now (works for any BLE device including BC01).
            # iBeacon UUID/major/minor parsing can be added later.
            beacon_id = device.address.upper()
            rssi = advertisement_data.rssi
            with self._lock:
                self._last_seen[beacon_id] = (rssi, time.monotonic())

        scanner = self._BleakScanner(detection_callback=on_advert)
        await scanner.start()
        try:
            while not self._stop.is_set():
                await self._asyncio.sleep(1.0)
        finally:
            await scanner.stop()


def _make_driver() -> BeaconDriver:
    name = os.getenv("PILOT_VIEW_BEACONS", "synthetic").strip().lower()
    if name == "bleak":
        try:
            return BleakBeaconDriver()
        except Exception as exc:
            print(f"[beacons] bleak init failed, falling back to synthetic: {exc}")
            return SyntheticBeaconDriver()
    return SyntheticBeaconDriver()


class Beacons:
    def __init__(self) -> None:
        self._driver = _make_driver()

    def snapshot(self) -> dict:
        return self._driver.snapshot()


beacons = Beacons()
