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
from scipy import ndimage

# Distinct colours for detected object clusters (RGB 0-1). Structure (walls/
# floor/ceiling) and noise are rendered grey.
_STRUCTURE = (0.42, 0.47, 0.55)
_PALETTE = [
    (1.00, 0.35, 0.35), (0.35, 0.85, 0.45), (0.40, 0.60, 1.00),
    (1.00, 0.80, 0.25), (0.85, 0.40, 1.00), (0.20, 0.90, 0.90),
    (1.00, 0.55, 0.20), (0.60, 1.00, 0.30), (1.00, 0.45, 0.75),
]


def _ransac_plane(P: np.ndarray, iters: int = 60, thresh: float = 0.035):
    """Return a boolean inlier mask for the dominant plane in P (Nx3)."""
    n = len(P)
    if n < 50:
        return np.zeros(n, dtype=bool)
    best_mask = np.zeros(n, dtype=bool)
    best_count = 0
    rng = np.random.default_rng(0)
    for _ in range(iters):
        i, j, k = rng.integers(0, n, 3)
        a, b, c = P[i], P[j], P[k]
        normal = np.cross(b - a, c - a)
        nn = np.linalg.norm(normal)
        if nn < 1e-6:
            continue
        normal = normal / nn
        dist = np.abs((P - a) @ normal)
        mask = dist < thresh
        cnt = int(mask.sum())
        if cnt > best_count:
            best_count = cnt
            best_mask = mask
    return best_mask


def _voxel_cluster(P: np.ndarray, voxel: float = 0.05) -> np.ndarray:
    """Label points into connected clusters via a voxel occupancy grid.
    Returns an int label per point (0 = unlabelled/empty)."""
    if len(P) == 0:
        return np.zeros(0, dtype=np.int32)
    mins = P.min(axis=0)
    idx = np.floor((P - mins) / voxel).astype(np.int64)
    shape = idx.max(axis=0) + 1
    occ = np.zeros(tuple(shape), dtype=bool)
    occ[idx[:, 0], idx[:, 1], idx[:, 2]] = True
    structure = np.ones((3, 3, 3), dtype=int)  # 26-connectivity
    labelled, _ = ndimage.label(occ, structure=structure)
    return labelled[idx[:, 0], idx[:, 1], idx[:, 2]].astype(np.int32)


# ── pure functions on a depth frame (used by both DepthCamera and OrbbecSource) ──

def _deproject(buf, scale, intr, step, min_range, max_range):
    """Depth frame -> Nx3 camera-frame points (x right, y down, z forward, metres)."""
    if buf is None or intr is None:
        return None
    fx, fy, cx, cy = intr
    sub = buf[::step, ::step].astype(np.float32)
    sh, sw = sub.shape
    us = (np.arange(sw) * step).astype(np.float32)
    vs = (np.arange(sh) * step).astype(np.float32)
    U, V = np.meshgrid(us, vs)
    Z = sub * scale / 1000.0
    mask = (Z > min_range) & (Z < max_range)
    if not mask.any():
        return None
    Zf, Uf, Vf = Z[mask], U[mask], V[mask]
    P = np.empty((Zf.size, 3), dtype=np.float32)
    P[:, 0] = (Uf - cx) * Zf / fx
    P[:, 1] = (Vf - cy) * Zf / fy
    P[:, 2] = Zf
    return P


def _to_threejs(P):
    """Camera frame (y down, z forward) -> Three.js (y up, -z forward)."""
    out = P.copy()
    out[:, 1] *= -1
    out[:, 2] *= -1
    return out


def cloud_bytes(buf, scale, intr, step=6, min_range=0.15, max_range=6.0) -> bytes:
    P = _deproject(buf, scale, intr, step, min_range, max_range)
    if P is None:
        return b""
    return _to_threejs(P).astype(np.float32).tobytes()


def segmented_bytes(buf, scale, intr, step=6, min_range=0.15, max_range=6.0,
                    max_planes=4, min_plane_frac=0.06, min_cluster=40) -> bytes:
    """[positions f32 N*3][colors f32 N*3], Three.js frame. Planes -> grey,
    discrete clusters -> distinct colours."""
    P = _deproject(buf, scale, intr, step, min_range, max_range)
    if P is None:
        return b""
    n = len(P)
    colors = np.tile(np.array(_STRUCTURE, dtype=np.float32), (n, 1))
    remaining = np.ones(n, dtype=bool)
    min_plane = max(60, int(n * min_plane_frac))
    for _ in range(max_planes):
        ridx = np.where(remaining)[0]
        if ridx.size < min_plane:
            break
        local = _ransac_plane(P[ridx])
        if int(local.sum()) < min_plane:
            break
        remaining[ridx[local]] = False
    obj_idx = np.where(remaining)[0]
    if obj_idx.size:
        labels = _voxel_cluster(P[obj_idx], voxel=0.05)
        uniq, counts = np.unique(labels, return_counts=True)
        big = sorted(
            ((int(c), int(u)) for u, c in zip(uniq, counts) if u != 0 and c >= min_cluster),
            reverse=True,
        )
        for rank, (_cnt, lab) in enumerate(big):
            colors[obj_idx[labels == lab]] = _PALETTE[rank % len(_PALETTE)]
    return _to_threejs(P).astype(np.float32).tobytes() + colors.astype(np.float32).tobytes()


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

    def get_depth(self):
        """Return (buf, scale, intr) for the latest frame, or None."""
        self.ensure_started()
        with self._lock:
            if self._latest is None or self._intr is None:
                return None
            return self._latest, self._scale, self._intr

    # ── point cloud ─────────────────────────────────────────────────────
    def point_cloud(self, **kw) -> bytes:
        d = self.get_depth()
        return cloud_bytes(*d, **kw) if d else b""

    def segmented_cloud(self, **kw) -> bytes:
        d = self.get_depth()
        return segmented_bytes(*d, **kw) if d else b""


depth_camera = DepthCamera()
