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


def _fit_plane(P, iters=120, thresh=0.03):
    """Dominant plane in P via RANSAC + least-squares refit.
    Returns (n_unit, d, inlier_mask) with the convention n·X + d = 0, or None."""
    n_pts = len(P)
    if n_pts < 80:
        return None
    rng = np.random.default_rng(0)
    best = None
    for _ in range(iters):
        i, j, k = rng.integers(0, n_pts, 3)
        a, b, c = P[i], P[j], P[k]
        nrm = np.cross(b - a, c - a)
        L = np.linalg.norm(nrm)
        if L < 1e-6:
            continue
        nrm = nrm / L
        d = -float(nrm.dot(a))
        cnt = int((np.abs(P.dot(nrm) + d) < thresh).sum())
        if best is None or cnt > best[0]:
            best = (cnt, nrm, d)
    if best is None:
        return None
    # Refit on inliers (PCA → normal is the smallest-variance direction).
    nrm, d = best[1], best[2]
    mask = np.abs(P.dot(nrm) + d) < thresh
    Q = P[mask]
    if len(Q) < 50:
        return nrm, d, mask
    centroid = Q.mean(axis=0)
    _, _, vt = np.linalg.svd(Q - centroid, full_matrices=False)
    nrm = vt[2] / np.linalg.norm(vt[2])
    d = -float(nrm.dot(centroid))
    mask = np.abs(P.dot(nrm) + d) < thresh
    return nrm, d, mask


def auto_pose_from_depth(buf, scale, intr, fov_deg=55.0, step=4):
    """Compute the camera pose in garage frame directly from depth: fit the floor
    (height + tilt) and the wall the camera faces (yaw + distance). Returns a
    live_view dict (camera_position/look_at/up/fov) + diagnostics, or None."""
    P = _deproject(buf, scale, intr, step, 0.2, 8.0)
    if P is None or len(P) < 500:
        return None

    # Peel several planes; orient each normal toward the camera origin (d > 0 so
    # the signed distance from the origin is +d).
    planes = []
    remaining = np.ones(len(P), dtype=bool)
    for _ in range(5):
        idx = np.where(remaining)[0]
        if idx.size < 400:
            break
        res = _fit_plane(P[idx])
        if res is None:
            break
        nrm, d, local = res
        if int(local.sum()) < 300:
            break
        if d < 0:
            nrm, d = -nrm, -d
        gidx = idx[local]
        planes.append({"n": nrm.astype(np.float64), "d": float(d), "count": int(local.sum()), "idx": gidx})
        remaining[gidx] = False
    if len(planes) < 2:
        return None

    # Up direction = consensus of all roughly-horizontal, up-pointing surfaces
    # (floor, desk, couch are all level, so their normals agree — robust to one
    # tilted clutter plane). Height = the LOWEST such surface = the real floor.
    horiz = [p for p in planes if p["n"][1] < -0.5]
    if horiz:
        acc = np.zeros(3)
        for p in horiz:
            acc += p["n"] * p["count"]
        up_cam = acc / np.linalg.norm(acc)
        h = max(p["d"] for p in horiz)
        floor = max(horiz, key=lambda p: p["d"])
    else:
        floor = max(planes, key=lambda p: (-p["n"][1]) * p["count"])
        up_cam = floor["n"] / np.linalg.norm(floor["n"])
        h = floor["d"]
    others = [p for p in planes if p is not floor]
    # Door/back wall = the FURTHEST wall the camera faces (the user's invariant:
    # the garage door is always the furthest plane). Among vertical planes in
    # front of the camera (normal points back toward it → n_z < 0), take the one
    # with the greatest distance. Falls back to "most head-on" if none qualify.
    forward_walls = [p for p in others if abs(p["n"][1]) < 0.5 and p["n"][2] < -0.2]
    if forward_walls:
        wall = max(forward_walls, key=lambda p: p["d"])
    else:
        wall = max(others, key=lambda p: abs(p["n"][2]) * p["count"] * (1.0 if abs(p["n"][1]) < 0.5 else 0.15))
    # Garage +Z (back wall → into room) in camera frame ≈ wall normal, made ⊥ to up.
    zc = wall["n"] - up_cam * float(wall["n"].dot(up_cam))
    zc = zc / np.linalg.norm(zc)
    xc = np.cross(up_cam, zc)
    xc = xc / np.linalg.norm(xc)
    R_cg = np.stack([xc, up_cam, zc], axis=1)   # columns = garage axes in cam frame
    R_gc = R_cg.T                               # camera vector → garage vector
    d_wall = wall["d"]

    # Centre laterally so the visible wall spans x=0.
    x0 = -float((P[wall["idx"]] @ xc).mean())
    pos = np.array([x0, h, d_wall], dtype=np.float64)
    forward = R_gc @ np.array([0.0, 0.0, 1.0])   # camera looks along +Z_cam
    up_g = R_gc @ np.array([0.0, -1.0, 0.0])     # camera up is -Y_cam
    look = pos + forward

    d3 = lambda v: {"x": float(v[0]), "y": float(v[1]), "z": float(v[2])}
    return {
        "camera_position": d3(pos),
        "camera_look_at": d3(look),
        "camera_up": d3(up_g),
        "camera_fov_deg": float(fov_deg),
        "floor_height": round(h, 3),
        "wall_distance": round(d_wall, 3),
        "floor_points": floor["count"],
        "wall_points": wall["count"],
        "planes_found": len(planes),
    }


def _floor_frame(P):
    """Find the floor and a gravity-aligned basis. Returns (up, d_floor, e1, e2)
    where up is the floor normal (toward camera), height(X)=up·X+d_floor, and
    e1,e2 span the floor plane. None if no floor found."""
    planes = []
    remaining = np.ones(len(P), dtype=bool)
    for _ in range(5):
        idx = np.where(remaining)[0]
        if idx.size < 400:
            break
        res = _fit_plane(P[idx])
        if res is None:
            break
        n, d, local = res
        if int(local.sum()) < 300:
            break
        if d < 0:
            n, d = -n, -d
        planes.append((n.astype(np.float64), float(d), idx[local]))
        remaining[idx[local]] = False
    horiz = [(n, d, gi) for (n, d, gi) in planes if n[1] < -0.5]
    if not horiz:
        return None
    acc = np.zeros(3)
    for n, d, gi in horiz:
        acc += n * len(gi)
    up = acc / np.linalg.norm(acc)
    d_floor = max(d for n, d, gi in horiz)
    ref = np.array([1.0, 0, 0]) if abs(up[0]) < 0.9 else np.array([0, 1.0, 0])
    e1 = np.cross(up, ref); e1 /= np.linalg.norm(e1)
    e2 = np.cross(up, e1)
    return up, d_floor, e1, e2


def _matches_vehicle(bb, expect):
    """Is this footprint roughly the expected vehicle size? Generous, because
    depth only sees the visible faces (partial footprint) — but a car is LONG,
    so the long-axis gate is what rejects people/boxes/clutter."""
    L, W, H = expect
    foot_long = max(bb["length"], bb["width"])
    foot_short = min(bb["length"], bb["width"])
    if not (0.55 * L <= foot_long <= 1.5 * L):
        return False
    if not (0.30 * W <= foot_short <= 1.9 * W):
        return False
    if bb["height"] > 1.7 * H + 0.4:
        return False
    return True


def detect_object(buf, scale, intr, expect=None, step=4, min_h=0.08, max_h=2.5,
                  voxel=0.05, min_pts=120, max_planes=10, min_plane=350, plane_thresh=0.05):
    """Biggest object standing on the floor → its footprint bounding box.
    Strips ALL structural planes (floor + walls) first, then clusters the
    leftover. Returns a dict with box dims + arrays for a debug render, or None."""
    P = _deproject(buf, scale, intr, step, 0.2, 8.0)
    if P is None or len(P) < 500:
        return None

    # Peel the dominant planes (floor + walls = structure).
    planes = []
    remaining = np.ones(len(P), dtype=bool)
    for _ in range(max_planes):
        idx = np.where(remaining)[0]
        if idx.size < min_plane:
            break
        res = _fit_plane(P[idx], thresh=plane_thresh)
        if res is None:
            break
        n, d, local = res
        if int(local.sum()) < min(min_plane, 300):
            break
        if d < 0:
            n, d = -n, -d
        planes.append((n.astype(np.float64), float(d), idx[local]))
        remaining[idx[local]] = False
    horiz = [(n, d, gi) for (n, d, gi) in planes if n[1] < -0.5]
    if not horiz:
        return None
    acc = np.zeros(3)
    for n, d, gi in horiz:
        acc += n * len(gi)
    up = acc / np.linalg.norm(acc)
    d_floor = max(d for n, d, gi in horiz)
    ref = np.array([1.0, 0, 0]) if abs(up[0]) < 0.9 else np.array([0, 1.0, 0])
    e1 = np.cross(up, ref); e1 /= np.linalg.norm(e1)
    e2 = np.cross(up, e1)

    a = P @ e1
    b = P @ e2
    height = P @ up + d_floor
    out = {"a": a, "b": b, "bbox": None}

    # Objects = leftover (non-plane) points standing on the floor.
    obj_idx = np.where(remaining)[0]
    h_obj = height[obj_idx]
    keep = (h_obj > min_h) & (h_obj < max_h)
    obj_idx = obj_idx[keep]
    if obj_idx.size < min_pts:
        return out

    labels = _voxel_cluster(P[obj_idx], voxel=voxel)
    uniq, counts = np.unique(labels, return_counts=True)
    valid = sorted(((int(c), int(u)) for u, c in zip(uniq, counts) if u != 0 and c >= min_pts), reverse=True)
    if not valid:
        return out

    def make_bbox(lab):
        g = obj_idx[labels == lab]
        ca, cb, ch = a[g], b[g], height[g]
        return g, {
            "a_min": float(ca.min()), "a_max": float(ca.max()),
            "b_min": float(cb.min()), "b_max": float(cb.max()),
            "length": float(ca.max() - ca.min()),
            "width": float(cb.max() - cb.min()),
            "height": float(ch.max()),
            "points": int(g.size),
        }

    # Pick the largest cluster that matches the expected vehicle size. If none
    # match (or no expectation given), fall back to the largest object so the
    # debug view still shows what was found, flagged as not-a-car.
    chosen = None
    fallback = None
    for _cnt, lab in valid[:8]:
        g, bb = make_bbox(lab)
        if fallback is None:
            fallback = (g, bb)
        if expect is not None and _matches_vehicle(bb, expect):
            chosen = (g, bb, True)
            break
    if chosen is None:
        g, bb = fallback
        chosen = (g, bb, False if expect is not None else None)

    g, bb, is_veh = chosen
    bb["is_vehicle"] = is_veh
    bb["expected"] = ({"length": expect[0], "width": expect[1], "height": expect[2]}
                      if expect is not None else None)
    out["cluster_global"] = g
    out["is_vehicle"] = is_veh
    out["bbox"] = bb
    return out


def _cam_to_garage_R(pose):
    """Rotation mapping OpenCV camera vectors → garage frame, from a saved
    live_view pose (camera_position/look_at/up). Returns (R_gc, C) or None."""
    try:
        C = np.array([pose["camera_position"]["x"], pose["camera_position"]["y"], pose["camera_position"]["z"]], float)
        L = np.array([pose["camera_look_at"]["x"], pose["camera_look_at"]["y"], pose["camera_look_at"]["z"]], float)
        U = np.array([pose["camera_up"]["x"], pose["camera_up"]["y"], pose["camera_up"]["z"]], float)
    except (KeyError, TypeError):
        return None
    f = L - C
    nf = np.linalg.norm(f)
    if nf < 1e-6:
        return None
    f = f / nf
    col1 = -U
    col1 = col1 - f * float(col1.dot(f))           # orthogonalise against forward
    n1 = np.linalg.norm(col1)
    if n1 < 1e-6:
        return None
    col1 /= n1
    col0 = np.cross(col1, f)
    col0 /= np.linalg.norm(col0)
    R_gc = np.stack([col0, col1, f], axis=1)        # columns = R_gc@(1,0,0),(0,1,0),(0,0,1)
    return R_gc, C


def detect_car_garage(buf, scale, intr, pose, garage, expect=None, step=4,
                      voxel=0.08, min_pts=150):
    """Detect the car in GARAGE coordinates. Transforms depth via the calibrated
    pose, keeps points inside the parking volume (floor + walls are the known
    bounds), clusters, and accepts the car-sized cluster. Returns
    {"car": {...}|None, "clearances": {...}}."""
    P = _deproject(buf, scale, intr, step, 0.2, 12.0)
    if P is None:
        return {"car": None, "clearances": {}}
    tr = _cam_to_garage_R(pose)
    if tr is None:
        return {"car": None, "clearances": {}}
    R_gc, C = tr
    Pg = P @ R_gc.T + C

    W = float(garage.get("width", 3.0))
    Ln = float(garage.get("length", 5.8))
    H = float(garage.get("height", 2.3))
    x, y, z = Pg[:, 0], Pg[:, 1], Pg[:, 2]
    m = 0.25
    inside = (y > 0.12) & (y < H + 0.2) & (np.abs(x) < W / 2 + m) & (z > -m) & (z < Ln + m)
    Pin = Pg[inside]
    if len(Pin) < min_pts:
        return {"car": None, "clearances": {}}

    labels = _voxel_cluster(Pin, voxel=voxel)
    uniq, counts = np.unique(labels, return_counts=True)
    valid = sorted(((int(c), int(u)) for u, c in zip(uniq, counts) if u != 0 and c >= min_pts), reverse=True)
    if not valid:
        return {"car": None, "clearances": {}}

    chosen = None
    for _cnt, lab in valid[:8]:
        Q = Pin[labels == lab]
        bb = {
            "length": float(np.ptp(Q[:, 2])),   # along the bay (z)
            "width": float(np.ptp(Q[:, 0])),    # across (x)
            "height": float(Q[:, 1].max()),     # up from the floor
        }
        if expect is None or _matches_vehicle(bb, expect):
            chosen = (Q, bb)
            break
    if chosen is None:
        return {"car": None, "clearances": {}}

    Q, bb = chosen
    cx = float((Q[:, 0].min() + Q[:, 0].max()) / 2)
    cz = float((Q[:, 2].min() + Q[:, 2].max()) / 2)
    # Use the KNOWN vehicle extent (depth only sees partial faces) so the box and
    # the GLB mesh are full-size and consistent; position comes from detection.
    if expect is not None:
        ext = {"length": float(expect[0]), "width": float(expect[1]), "height": float(expect[2])}
    else:
        ext = {"length": bb["length"], "width": bb["width"], "height": bb["height"]}
    hl, hw = ext["length"] / 2, ext["width"] / 2
    car = {
        "position": {"x": cx, "y": ext["height"] / 2, "z": cz},
        "yaw": 0.0,
        "extent": ext,
        "measured": {"length": bb["length"], "width": bb["width"], "height": bb["height"]},
    }
    clearances = {
        "front": Ln - (cz + hl),
        "rear": cz - hl,
        "left": W / 2 + cx - hw,
        "right": W / 2 - cx - hw,
        "ceiling": H - ext["height"],
    }
    return {"car": car, "clearances": clearances}


def detect_debug_png(buf, scale, intr, expect=None):
    """Top-down (bird's-eye) PNG: all points grey, the detected object cyan, its
    bounding box green if it matches the expected vehicle size, amber if not.
    Empty bytes if nothing/no camera."""
    import cv2
    res = detect_object(buf, scale, intr, expect=expect)
    if res is None:
        return b""
    a, b = res["a"], res["b"]
    amin, amax, bmin, bmax = float(a.min()), float(a.max()), float(b.min()), float(b.max())
    W = H = 640
    pad = 30
    s = min((W - 2 * pad) / (amax - amin + 1e-6), (H - 2 * pad) / (bmax - bmin + 1e-6))
    img = np.zeros((H, W, 3), dtype=np.uint8)

    def px(av, bv):
        return (np.clip((pad + (av - amin) * s), 0, W - 1).astype(np.int32),
                np.clip((pad + (bv - bmin) * s), 0, H - 1).astype(np.int32))

    xs, ys = px(a, b)
    img[ys, xs] = (70, 70, 80)
    if res.get("cluster_global") is not None:
        gi = res["cluster_global"]
        cx, cy = px(a[gi], b[gi])
        img[cy, cx] = (255, 210, 90)
    bb = res.get("bbox")
    if bb:
        is_veh = bb.get("is_vehicle")
        color = (120, 255, 90) if is_veh else ((0, 165, 255) if is_veh is False else (255, 210, 90))
        (x0, y0) = (int(pad + (bb["a_min"] - amin) * s), int(pad + (bb["b_min"] - bmin) * s))
        (x1, y1) = (int(pad + (bb["a_max"] - amin) * s), int(pad + (bb["b_max"] - bmin) * s))
        cv2.rectangle(img, (x0, y0), (x1, y1), color, 2)
        dims = f"{bb['length']:.2f} x {bb['width']:.2f} m  h {bb['height']:.2f}"
        if is_veh is True:
            label = "CAR  " + dims
        elif is_veh is False:
            ex = bb.get("expected") or {}
            label = f"not car-sized: {dims}  (need ~{ex.get('length', 0):.1f}x{ex.get('width', 0):.1f})"
        else:
            label = dims
        cv2.putText(img, label, (10, H - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)
    cv2.putText(img, "top-down (bird's-eye)", (10, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (150, 160, 180), 1, cv2.LINE_AA)
    ok, enc = cv2.imencode(".png", img)
    return enc.tobytes() if ok else b""


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
