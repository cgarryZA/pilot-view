"""Depth capture + point-cloud deprojection for the Orbbec Gemini 2.

Owns a depth-only pipeline (no colour, so it sidesteps the Windows Media
Foundation path and USB bandwidth pressure), keeps the latest depth frame, and
deprojects it to a 3D point cloud using the depth intrinsics:

    X = (u - cx) * Z / fx
    Y = (v - cy) * Z / fy
    Z = depth

Points are returned in a Three.js-friendly frame (x right, y up, z toward the
viewer) as a flat little-endian float32 buffer [x0,y0,z0, x1,y1,z1, ...] so the
browser can drop them straight into a BufferGeometry.

Lazy: the camera isn't opened until the first point_cloud() call, so importing
this module is free on machines without the camera/SDK.
"""

import threading
import time
from typing import Optional

import numpy as np


class DepthCamera:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: Optional[np.ndarray] = None  # HxW uint16
        self._scale: float = 1.0                    # mm per depth unit
        self._intr = None                           # fx, fy, cx, cy
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._error: Optional[str] = None
        self._started = False

    # ── lifecycle ───────────────────────────────────────────────────────
    def ensure_started(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        try:
            import pyorbbecsdk as ob
        except Exception as exc:  # pragma: no cover
            with self._lock:
                self._error = f"pyorbbecsdk import failed: {exc}"
                self._running = False
            return

        while self._running:
            pipe = None
            try:
                pipe = ob.Pipeline()
                cfg = ob.Config()
                profiles = pipe.get_stream_profile_list(ob.OBSensorType.DEPTH_SENSOR)
                dp = profiles.get_default_video_stream_profile()
                cfg.enable_stream(dp)
                intr = dp.get_intrinsic()
                with self._lock:
                    self._intr = (float(intr.fx), float(intr.fy), float(intr.cx), float(intr.cy))
                    self._error = None
                pipe.start(cfg)

                while self._running:
                    frames = pipe.wait_for_frames(300)
                    if frames is None:
                        continue
                    d = frames.get_depth_frame()
                    if d is None:
                        continue
                    w, h = d.get_width(), d.get_height()
                    buf = np.frombuffer(d.get_data(), dtype=np.uint16).reshape((h, w)).copy()
                    with self._lock:
                        self._latest = buf
                        self._scale = float(d.get_depth_scale())
            except Exception as exc:
                with self._lock:
                    self._error = f"{type(exc).__name__}: {exc}"
                    self._latest = None
            finally:
                if pipe is not None:
                    try:
                        pipe.stop()
                    except Exception:
                        pass
            if self._running:
                time.sleep(2.0)

    def status(self) -> dict:
        with self._lock:
            return {
                "running": self._running,
                "has_frame": self._latest is not None,
                "error": self._error,
                "intrinsics": self._intr,
            }

    # ── point cloud ─────────────────────────────────────────────────────
    def point_cloud(self, step: int = 6, min_range: float = 0.15,
                    max_range: float = 6.0) -> bytes:
        """Deproject the latest depth frame to a downsampled point cloud.
        Returns flat float32 xyz bytes (Three.js frame). Empty if no frame."""
        self.ensure_started()
        with self._lock:
            buf = self._latest
            scale = self._scale
            intr = self._intr
        if buf is None or intr is None:
            return b""

        fx, fy, cx, cy = intr
        h, w = buf.shape
        sub = buf[::step, ::step].astype(np.float32)
        sh, sw = sub.shape
        us = (np.arange(sw) * step).astype(np.float32)
        vs = (np.arange(sh) * step).astype(np.float32)
        U, V = np.meshgrid(us, vs)

        Z = sub * scale / 1000.0  # metres
        mask = (Z > min_range) & (Z < max_range)
        if not mask.any():
            return b""

        Zf = Z[mask]
        Xf = (U[mask] - cx) * Zf / fx
        Yf = (V[mask] - cy) * Zf / fy
        # Camera (OpenCV: x right, y down, z forward) -> Three.js (y up, -z forward)
        pts = np.empty((Zf.size, 3), dtype=np.float32)
        pts[:, 0] = Xf
        pts[:, 1] = -Yf
        pts[:, 2] = -Zf
        return pts.tobytes()


depth_camera = DepthCamera()
