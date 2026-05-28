"""Orbbec Gemini 2 source — RGB capture.

A background thread opens the camera pipeline, grabs colour frames, converts
them to JPEG, and stores the latest frame. The MJPEG endpoint in main.py serves
those bytes. state() reports the device as connected once frames are flowing and
emits the calibrated garage geometry so the wireframe overlays the live feed
(car detection from depth comes later).

Robust to hot-plug: if the pipeline errors (cable yanked, USB hiccup) the loop
catches it, clears the latest frame, and retries every few seconds.
"""

import math
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from app import calibration
from app.sources.base import CameraSource

RECONNECT_DELAY_SECONDS = 3.0
FRAME_STALE_SECONDS = 2.0  # if no frame in this long, treat as not-streaming

# Preferred colour profile — MJPG keeps bandwidth low enough for USB 2.0.
PREFERRED = [
    (1280, 720, "MJPG", 30),
    (640, 480, "MJPG", 30),
    (1280, 720, "MJPG", 15),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class OrbbecSource(CameraSource):
    name = "orbbec"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._device_info: Optional[dict] = None
        self._last_attempt: Optional[str] = None
        self._last_error: Optional[str] = None
        self._latest_jpeg: Optional[bytes] = None
        self._last_frame_monotonic: float = 0.0
        self._fov_deg: Optional[float] = None   # vertical FOV from camera intrinsics
        self._running = False
        self._thread: Optional[threading.Thread] = None

        try:
            import pyorbbecsdk  # type: ignore
            import cv2  # type: ignore
            import numpy as np  # type: ignore
            self._sdk = pyorbbecsdk
            self._cv2 = cv2
            self._np = np
            self._sdk_loaded = True
        except Exception as exc:  # pragma: no cover — environmental
            self._sdk_loaded = False
            self._last_error = f"pyorbbecsdk import failed: {exc}"
            return

        self._running = True
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

    # ── capture thread ──────────────────────────────────────────────────

    def _capture_loop(self) -> None:
        sdk = self._sdk
        while self._running:
            pipeline = None
            try:
                with self._lock:
                    self._last_attempt = _now()

                pipeline = sdk.Pipeline()
                self._read_device_info(pipeline)

                config = sdk.Config()
                color_profile = self._pick_color_profile(pipeline)
                config.enable_stream(color_profile)
                self._read_intrinsics(color_profile)
                pipeline.start(config)

                with self._lock:
                    self._last_error = None

                # Inner frame loop
                while self._running:
                    frames = pipeline.wait_for_frames(200)
                    if frames is None:
                        continue
                    color = frames.get_color_frame()
                    if color is None:
                        continue
                    bgr = self._frame_to_bgr(color)
                    if bgr is None:
                        continue
                    ok, buf = self._cv2.imencode(
                        ".jpg", bgr, [int(self._cv2.IMWRITE_JPEG_QUALITY), 80]
                    )
                    if ok:
                        with self._lock:
                            self._latest_jpeg = buf.tobytes()
                            self._last_frame_monotonic = time.monotonic()
            except Exception as exc:
                with self._lock:
                    self._device_info = None
                    self._latest_jpeg = None
                    self._last_error = f"{type(exc).__name__}: {exc}"
            finally:
                if pipeline is not None:
                    try:
                        pipeline.stop()
                    except Exception:
                        pass
            if self._running:
                time.sleep(RECONNECT_DELAY_SECONDS)

    def _read_device_info(self, pipeline) -> None:
        try:
            device = pipeline.get_device()
            info = device.get_device_info()
            with self._lock:
                self._device_info = {
                    "model": info.get_name(),
                    "serial": info.get_serial_number(),
                    "firmware": info.get_firmware_version(),
                }
        except Exception as exc:
            with self._lock:
                self._last_error = f"device info read failed: {exc}"

    def _read_intrinsics(self, color_profile) -> None:
        """Pull the camera's real vertical FOV from the colour stream intrinsics,
        so pose calibration can skip solving for FOV and use the true value."""
        try:
            intr = color_profile.get_intrinsic()
            fy = float(intr.fy)
            h = float(intr.height)
            if fy > 0 and h > 0:
                fov = math.degrees(2.0 * math.atan((h / 2.0) / fy))
                with self._lock:
                    self._fov_deg = round(fov, 2)
        except Exception as exc:
            print(f"[orbbec] could not read intrinsics: {exc}")

    def _pick_color_profile(self, pipeline):
        sdk = self._sdk
        profiles = pipeline.get_stream_profile_list(sdk.OBSensorType.COLOR_SENSOR)
        for (w, h, fmt_name, fps) in PREFERRED:
            try:
                fmt = getattr(sdk.OBFormat, fmt_name)
                p = profiles.get_video_stream_profile(w, h, fmt, fps)
                if p is not None:
                    return p
            except Exception:
                continue
        # Fall back to whatever the SDK considers default.
        return profiles.get_default_video_stream_profile()

    def _frame_to_bgr(self, frame):
        sdk, cv2, np = self._sdk, self._cv2, self._np
        try:
            w = frame.get_width()
            h = frame.get_height()
            fmt = frame.get_format()
            data = np.frombuffer(frame.get_data(), dtype=np.uint8)
            OBFormat = sdk.OBFormat

            if fmt == OBFormat.MJPG:
                return cv2.imdecode(data, cv2.IMREAD_COLOR)
            if fmt == OBFormat.RGB:
                return cv2.cvtColor(data.reshape((h, w, 3)), cv2.COLOR_RGB2BGR)
            if fmt == OBFormat.BGR:
                return data.reshape((h, w, 3))
            if fmt == OBFormat.YUYV:
                return cv2.cvtColor(data.reshape((h, w, 2)), cv2.COLOR_YUV2BGR_YUYV)
            if fmt == OBFormat.UYVY:
                return cv2.cvtColor(data.reshape((h, w, 2)), cv2.COLOR_YUV2BGR_UYVY)
            # Last resort: assume it's a JPEG payload.
            return cv2.imdecode(data, cv2.IMREAD_COLOR)
        except Exception as exc:
            print(f"[orbbec] frame conversion failed: {exc}")
            return None

    # ── public surface ──────────────────────────────────────────────────

    def get_jpeg(self) -> Optional[bytes]:
        with self._lock:
            return self._latest_jpeg

    def close(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=3)

    def _streaming(self) -> bool:
        with self._lock:
            return (
                self._latest_jpeg is not None
                and (time.monotonic() - self._last_frame_monotonic) < FRAME_STALE_SECONDS
            )

    def state(self) -> dict:
        cal = calibration.load()
        garage = cal.get("garage", {})
        thresholds = cal.get("thresholds", {"warn": 0.5, "danger": 0.2})

        streaming = self._streaming()
        with self._lock:
            info = self._device_info
            error = self._last_error
            last_attempt = self._last_attempt
            fov_deg = self._fov_deg

        geometry = None
        live_url = None
        if streaming:
            live_url = "/api/camera/stream"
            # Emit garage geometry from calibration so the wireframe walls + door
            # overlay the live feed. Car stays null until depth detection lands.
            geometry = {
                "garage": {
                    "width": garage.get("width", 3.0),
                    "length": garage.get("length", 5.8),
                    "height": garage.get("height", 2.3),
                    "door_opening_width": garage.get("door_opening_width", 2.4),
                    "door_opening_height": garage.get("door_opening_height", 2.0),
                },
                "car": None,
                "clearances": {},
                "thresholds": {
                    "warn": thresholds.get("warn", 0.5),
                    "danger": thresholds.get("danger", 0.2),
                },
                "state": "safe",
            }

        connected = info is not None
        return {
            "source": self.name,
            "ts": _now(),
            "live_url": live_url,
            "camera": {
                "connected": connected and streaming,
                "model": info["model"] if info else "Orbbec Gemini 2",
                "interface": "USB",
                "serial": info["serial"] if info else None,
                "firmware": info["firmware"] if info else None,
                "fov_deg": fov_deg,   # real vertical FOV from intrinsics (None if unknown)
                "last_attempt": last_attempt,
                "error": None if streaming else (error or "waiting for frames"),
            },
            "geometry": geometry,
        }
