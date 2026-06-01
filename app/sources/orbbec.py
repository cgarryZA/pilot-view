"""Orbbec Gemini 2 source — RGB capture.

A background thread opens the camera pipeline, grabs colour frames, converts
them to JPEG, and stores the latest frame. The MJPEG endpoint in main.py serves
those bytes. state() reports the device as connected once frames are flowing and
emits the calibrated garage geometry so the wireframe overlays the live feed
(car detection from depth comes later).

Robust to hot-plug: if the pipeline errors (cable yanked, USB hiccup) the loop
catches it, clears the latest frame, and retries every few seconds.
"""

import copy
import logging
import math
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from app import calibration
from app.sources.base import CameraSource

logger = logging.getLogger("pilot_view.orbbec")

RECONNECT_DELAY_SECONDS = 3.0
FRAME_STALE_SECONDS = 2.0  # if no frame in this long, treat as not-streaming
DETECT_ACTIVE_SECONDS = 0.35   # poll cadence while a car is tracked
DETECT_IDLE_SECONDS = 1.5      # backed-off cadence when the bay is empty
STOPPED_SPEED_MPS = 0.03       # below this the car counts as parked (not moving)

# Preferred colour profile — MJPG keeps bandwidth low enough for USB 2.0.
PREFERRED = [
    (1280, 720, "MJPG", 30),
    (640, 480, "MJPG", 30),
    (1280, 720, "MJPG", 15),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def quiet_orbbec_logs(sdk) -> None:
    """Turn the OrbbecSDK's own logging down to ERROR. By default it writes
    per-frame info/warning lines to Log/OrbbecSDK.log.txt continuously (hundreds
    of MB/day on a 24/7 deployment → eventually fills the disk). The exact API
    name varies across pyorbbecsdk versions, so probe defensively."""
    try:
        sev = None
        for enum_name in ("OBLogLevel", "OBLogSeverity"):
            enum = getattr(sdk, enum_name, None)
            if enum is None:
                continue
            for attr in ("ERROR", "OB_LOG_SEVERITY_ERROR", "OB_LOG_LEVEL_ERROR"):
                sev = getattr(enum, attr, None)
                if sev is not None:
                    break
            if sev is not None:
                break
        ctx = getattr(sdk, "Context", None)
        if ctx is None or sev is None:
            return
        for meth in ("set_logger_to_console", "set_logger_severity", "set_logger_level"):
            fn = getattr(ctx, meth, None)
            if callable(fn):
                try:
                    fn(sev)
                    return
                except Exception:
                    continue
    except Exception:
        pass


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
        self._latest_depth = None               # HxW uint16
        self._depth_scale: float = 1.0          # mm per depth unit
        self._depth_intr = None                 # (fx, fy, cx, cy)
        self._car = None                        # detected car (garage frame) or None
        self._clearances: dict = {}             # per-side clearances (m)
        self._moving = True                     # is the car moving? (drives "parked")
        self._last_pos = None                   # last smoothed (x, z) for speed est.
        self._last_pos_ts = 0.0
        self._gate_warned = False               # throttle the size-gate-unavailable log
        self._vehicles_logged = False           # log vehicles driver once
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._det_thread: Optional[threading.Thread] = None

        try:
            import pyorbbecsdk  # type: ignore
            import cv2  # type: ignore
            import numpy as np  # type: ignore
            self._sdk = pyorbbecsdk
            self._cv2 = cv2
            self._np = np
            self._sdk_loaded = True
            quiet_orbbec_logs(pyorbbecsdk)
        except Exception as exc:  # pragma: no cover — environmental
            self._sdk_loaded = False
            self._last_error = f"pyorbbecsdk import failed: {exc}"
            return

        self._running = True
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()
        # Car detection runs in its own (throttled) thread so the heavy
        # RANSAC/clustering never stalls the frame grab.
        self._det_thread = threading.Thread(target=self._detection_loop, daemon=True)
        self._det_thread.start()

    # ── car detection thread ────────────────────────────────────────────

    def _expected_extent(self):
        """(length, width, height) of the active vehicle, or None. Logged on
        failure — a silently-missing extent would disable the size gate."""
        try:
            from app.vehicles import vehicles
            if not self._vehicles_logged:
                logger.info("vehicles driver=%s", vehicles.driver_name)
                self._vehicles_logged = True
            ext = (vehicles.state_dict().get("active") or {}).get("extent")
            if ext and all(k in ext for k in ("length", "width", "height")):
                return (float(ext["length"]), float(ext["width"]), float(ext["height"]))
        except Exception:
            logger.exception("active-vehicle extent lookup failed")
        return None

    def _update_motion(self, pos) -> None:
        """Estimate whether the car is moving from successive smoothed positions
        (caller holds the lock). Drives the 'parked' (stopped + clear) state."""
        now = time.monotonic()
        if self._last_pos is not None:
            dt = now - self._last_pos_ts
            if dt > 1e-3:
                dx = pos["x"] - self._last_pos[0]
                dz = pos["z"] - self._last_pos[1]
                speed = (dx * dx + dz * dz) ** 0.5 / dt
                self._moving = speed > STOPPED_SPEED_MPS
        self._last_pos = (pos["x"], pos["z"])
        self._last_pos_ts = now

    def _detection_loop(self) -> None:
        from app import calibration, depth as depth_mod
        miss = 0
        while self._running:
            # Idle backoff: poll fast while a car is tracked (the drive-in moment
            # that matters), slow when the bay is empty so we aren't running
            # RANSAC + clustering several times a second, 24/7.
            with self._lock:
                tracking = self._car is not None
            time.sleep(DETECT_ACTIVE_SECONDS if tracking else DETECT_IDLE_SECONDS)

            d = self.get_depth()
            if d is None:
                continue
            try:
                cal = calibration.load()
                pose = cal.get("live_view")
                garage = cal.get("garage", {})
                if not pose:
                    continue

                expect = self._expected_extent()
                if expect is None:
                    # Without a known vehicle extent the size gate can't run.
                    # Refuse to detect ungated (a person/bin/ladder would be
                    # accepted as "the car"); treat as no car and warn once.
                    if not self._gate_warned:
                        logger.warning("size gate unavailable (no active-vehicle extent); detection paused")
                        self._gate_warned = True
                    new_car = None
                else:
                    self._gate_warned = False
                    res = depth_mod.detect_car_garage(*d, pose=pose, garage=garage, expect=expect)
                    new_car = res.get("car") if res else None

                with self._lock:
                    if new_car is not None:
                        npp = new_car["position"]
                        if self._car is not None:   # EMA-smooth so the box doesn't jitter
                            a = 0.4
                            op = self._car["position"]
                            npp["x"] = op["x"] + a * (npp["x"] - op["x"])
                            npp["z"] = op["z"] + a * (npp["z"] - op["z"])
                        # clearances from the smoothed position
                        ext = new_car["extent"]
                        W = float(garage.get("width", 3.0))
                        Ln = float(garage.get("length", 5.8))
                        H = float(garage.get("height", 2.3))
                        hl, hw = ext["length"] / 2, ext["width"] / 2
                        self._clearances = {
                            "front": Ln - (npp["z"] + hl),
                            "rear": npp["z"] - hl,
                            "left": W / 2 + npp["x"] - hw,
                            "right": W / 2 - npp["x"] - hw,
                            "ceiling": H - ext["height"],
                        }
                        self._update_motion(npp)
                        self._car = new_car
                        miss = 0
                    else:                            # hysteresis: don't drop on a single miss
                        miss += 1
                        if miss >= 3:
                            self._car = None
                            self._clearances = {}
                            self._moving = True
                            self._last_pos = None
            except Exception:
                logger.exception("detection loop error")

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
                # Depth too — feeds the point cloud / car detection. Best-effort:
                # if depth can't be enabled we still stream colour.
                depth_profile = self._pick_depth_profile(pipeline)
                if depth_profile is not None:
                    try:
                        config.enable_stream(depth_profile)
                        self._read_depth_intrinsics(depth_profile)
                    except Exception as exc:
                        print(f"[orbbec] depth enable failed: {exc}")
                pipeline.start(config)

                with self._lock:
                    self._last_error = None

                # Inner frame loop — handle colour and depth independently.
                while self._running:
                    frames = pipeline.wait_for_frames(200)
                    if frames is None:
                        continue
                    color = frames.get_color_frame()
                    if color is not None:
                        bgr = self._frame_to_bgr(color)
                        if bgr is not None:
                            ok, buf = self._cv2.imencode(
                                ".jpg", bgr, [int(self._cv2.IMWRITE_JPEG_QUALITY), 80]
                            )
                            if ok:
                                with self._lock:
                                    self._latest_jpeg = buf.tobytes()
                                    self._last_frame_monotonic = time.monotonic()
                    depth = frames.get_depth_frame()
                    if depth is not None:
                        try:
                            dh, dw = depth.get_height(), depth.get_width()
                            dbuf = self._np.frombuffer(
                                depth.get_data(), dtype=self._np.uint16
                            ).reshape((dh, dw)).copy()
                            with self._lock:
                                self._latest_depth = dbuf
                                self._depth_scale = float(depth.get_depth_scale())
                        except Exception:
                            pass
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

    def _pick_depth_profile(self, pipeline):
        sdk = self._sdk
        try:
            profiles = pipeline.get_stream_profile_list(sdk.OBSensorType.DEPTH_SENSOR)
            return profiles.get_default_video_stream_profile()
        except Exception as exc:
            print(f"[orbbec] no depth profile: {exc}")
            return None

    def _read_depth_intrinsics(self, depth_profile) -> None:
        try:
            intr = depth_profile.get_intrinsic()
            with self._lock:
                self._depth_intr = (
                    float(intr.fx), float(intr.fy), float(intr.cx), float(intr.cy)
                )
        except Exception as exc:
            print(f"[orbbec] could not read depth intrinsics: {exc}")

    def get_depth(self):
        """(buf, scale, intr) for the latest depth frame, or None."""
        with self._lock:
            if self._latest_depth is None or self._depth_intr is None:
                return None
            return self._latest_depth, self._depth_scale, self._depth_intr

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
        if self._det_thread is not None:
            self._det_thread.join(timeout=2)

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
        # Has the camera pose actually been calibrated (auto-align / pose solve),
        # versus the bare DEFAULTS pose? Drives "uncalibrated" vs "searching".
        calibrated = bool(cal.get("live_view", {}).get("calibrated"))
        with self._lock:
            info = self._device_info
            error = self._last_error
            last_attempt = self._last_attempt
            fov_deg = self._fov_deg
            # Deep-copy so the serialized snapshot can't tear if the detection
            # thread mutates self._car in place between here and JSON encoding.
            car = copy.deepcopy(self._car) if self._car else None
            clearances = dict(self._clearances)
            moving = self._moving

        warn = thresholds.get("warn", 0.5)
        danger = thresholds.get("danger", 0.2)
        if clearances:
            smallest = min(clearances.values())
            if smallest < danger:
                state_label = "danger"
            elif smallest < warn:
                state_label = "warning"
            elif not moving:
                state_label = "parked"      # stopped AND clear → OK to get out
            else:
                state_label = "safe"        # clear but still moving
        else:
            # Streaming but no car-sized cluster found. NEVER show this as green
            # "safe" — that's indistinguishable from "all clear, keep going".
            state_label = "searching" if calibrated else "uncalibrated"

        geometry = None
        live_url = None
        if streaming:
            live_url = "/api/camera/stream"
            # Garage wireframe from calibration; car + clearances from the depth
            # detection thread (null until a car-sized object is found).
            geometry = {
                "garage": {
                    "width": garage.get("width", 3.0),
                    "length": garage.get("length", 5.8),
                    "height": garage.get("height", 2.3),
                    "door_opening_width": garage.get("door_opening_width", 2.4),
                    "door_opening_height": garage.get("door_opening_height", 2.0),
                },
                "car": car,
                "clearances": clearances,
                "thresholds": {"warn": warn, "danger": danger},
                "state": state_label,
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
