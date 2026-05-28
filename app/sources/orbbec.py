"""Orbbec Gemini 2 source — skeleton.

Reports the camera as connected/disconnected based on whether the SDK can see
a Gemini 2 (or compatible Orbbec depth camera) on the USB bus. Doesn't grab
frames or compute geometry yet — that work lands once the hardware is mounted
and we can iterate against real data.

While unplugged, this source behaves like DisconnectedSource: state() reports
camera connected=False with the last_attempt timestamp. As soon as a device
appears on the USB bus, the next poll picks it up and reports connected=True.
"""

import threading
import time
from datetime import datetime, timezone
from typing import Optional

from app.sources.base import CameraSource

# Re-poll the USB bus this often when no device is present (hot-plug detection).
RECONNECT_POLL_SECONDS = 2.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class OrbbecSource(CameraSource):
    name = "orbbec"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._device_info: Optional[dict] = None
        self._last_attempt: Optional[str] = None
        self._last_error: Optional[str] = None
        self._next_probe: float = 0.0

        # Lazy import — the SDK may not be installed on dev machines.
        try:
            import pyorbbecsdk  # type: ignore
            self._sdk = pyorbbecsdk
            self._context = pyorbbecsdk.Context()
            self._sdk_loaded = True
        except Exception as exc:  # pragma: no cover — environmental
            self._sdk = None
            self._context = None
            self._sdk_loaded = False
            self._last_error = f"pyorbbecsdk import failed: {exc}"

        # Initial probe (non-blocking — just looks at the USB bus).
        self._probe()

    def _probe(self) -> None:
        if not self._sdk_loaded:
            return
        now = time.monotonic()
        if now < self._next_probe:
            return
        self._next_probe = now + RECONNECT_POLL_SECONDS

        try:
            devices = self._context.query_devices()
            count = devices.get_count()
        except Exception as exc:
            with self._lock:
                self._device_info = None
                self._last_attempt = _now()
                self._last_error = f"query_devices failed: {exc}"
            return

        with self._lock:
            self._last_attempt = _now()
            if count == 0:
                self._device_info = None
                self._last_error = "no Orbbec device on USB bus"
                return

            # Pick first device — for v1 we only care about a single camera.
            try:
                device = devices.get_device_by_index(0)
                info = device.get_device_info()
                self._device_info = {
                    "model": info.get_name(),
                    "serial": info.get_serial_number(),
                    "firmware": info.get_firmware_version(),
                    "pid": info.get_pid(),
                    "vid": info.get_vid(),
                }
                self._last_error = None
            except Exception as exc:
                self._device_info = None
                self._last_error = f"failed to read device info: {exc}"

    def state(self) -> dict:
        self._probe()
        with self._lock:
            info = self._device_info
            connected = info is not None
            payload = {
                "source": self.name,
                "ts": _now(),
                "live_url": None,  # MJPEG stream URL will go here once frames are wired
                "camera": {
                    "connected": connected,
                    "model": info["model"] if connected else "Orbbec Gemini 2",
                    "interface": "USB 3.0",
                    "serial": info["serial"] if connected else None,
                    "firmware": info["firmware"] if connected else None,
                    "last_attempt": self._last_attempt,
                    "error": None if connected else self._last_error,
                },
                # Geometry stays None until we actually process depth frames.
                "geometry": None,
            }
        return payload
