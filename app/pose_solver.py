"""Camera-pose + FOV solver from a known cuboid and its receding edges.

Node system (matches the UI):
  * BLUE  — the 4 exact corners of the back/door wall (a known W×H rectangle on z=0).
  * WHITE — direction-only nodes. The line BLUE→WHITE is the image direction of the
            receding edge (the wall↔floor / wall↔ceiling seam). The true near
            corner is off-screen at  blue_world + (0,0,length); only the WHITE
            node's *direction* from its blue corner is meaningful.
  * YELLOW — the 4 exact corners of the garage-door opening (used for door dims).

How FOV is found: the four receding edges are all parallel (+Z) and the room is
rectangular (90° corners). For a candidate FOV we solve the camera pose from the
back-wall rectangle (IPPE — a planar target), project the near corners (z=length),
and measure how well each projected receding edge's *direction* matches the user's
BLUE→WHITE direction. The FOV that minimises that angular error is the answer.
This is why FOV is computed, not slid.

If the caller supplies a known FOV (e.g. read from the real Gemini 2's
intrinsics), we skip the search and just solve the pose at that FOV.

Returns a Three.js-ready (position, look_at, up). up is derived from the solved
rotation so the OpenCV(+Y-down) → Three.js(+Y-up) conversion is exact.
"""

from typing import Dict, List, Optional, Tuple

import numpy as np
import cv2

# Pin order for back + near (paired): top-left, top-right, bottom-right, bottom-left.
CORNER_ORDER = ["tl", "tr", "br", "bl"]
DOOR_ORDER = ["door_tl", "door_tr", "door_br", "door_bl"]


def back_wall_world(width: float, height: float) -> np.ndarray:
    w = width / 2.0
    return np.array([
        [-w, height, 0.0],  # tl
        [ w, height, 0.0],  # tr
        [ w, 0.0,    0.0],  # br
        [-w, 0.0,    0.0],  # bl
    ], dtype=np.float64)


def near_wall_world(width: float, height: float, length: float) -> np.ndarray:
    w = width / 2.0
    return np.array([
        [-w, height, length],
        [ w, height, length],
        [ w, 0.0,    length],
        [-w, 0.0,    length],
    ], dtype=np.float64)


def door_corners_world(door_w: float, door_h: float, center_x: float = 0.0) -> Dict[str, Tuple[float, float, float]]:
    half = door_w / 2.0
    return {
        "door_tl": (center_x - half, door_h, 0.0),
        "door_tr": (center_x + half, door_h, 0.0),
        "door_br": (center_x + half, 0.0,    0.0),
        "door_bl": (center_x - half, 0.0,    0.0),
    }


def intrinsics(image_size_px: Tuple[int, int], fov_deg: float) -> np.ndarray:
    w, h = image_size_px
    fov_rad = np.deg2rad(fov_deg)
    fy = (h / 2.0) / np.tan(fov_rad / 2.0)
    fx = fy
    return np.array([[fx, 0, w / 2.0], [0, fy, h / 2.0], [0, 0, 1.0]], dtype=np.float64)


def _ippe_back_wall(back_img: np.ndarray, back_world: np.ndarray, K: np.ndarray):
    """Pose from the back-wall rectangle; pick the physically valid mirror solution."""
    n, rvecs, tvecs, reproj = cv2.solvePnPGeneric(
        back_world, back_img, K, np.zeros(4), flags=cv2.SOLVEPNP_IPPE
    )
    if n < 1:
        return None
    best = None
    for i in range(n):
        R, _ = cv2.Rodrigues(rvecs[i])
        cam = (-R.T @ tvecs[i]).flatten()
        # reproj[i] is an array (numpy 2.x rejects float() on non-0d arrays).
        err = float(np.ravel(reproj[i])[0]) if reproj is not None else 0.0
        score = (1000.0 if cam[1] > 0 else 0.0) - err   # prefer camera above floor
        if best is None or score > best[0]:
            best = (score, rvecs[i], tvecs[i])
    return best[1], best[2]


def _edge_direction_error(K, rvec, tvec, back_world, near_world, drawn_dir) -> float:
    bp, _ = cv2.projectPoints(back_world, rvec, tvec, K, np.zeros(4))
    npj, _ = cv2.projectPoints(near_world, rvec, tvec, K, np.zeros(4))
    bp = bp.reshape(-1, 2)
    npj = npj.reshape(-1, 2)
    total = 0.0
    for i in range(4):
        d = npj[i] - bp[i]
        nn = np.linalg.norm(d)
        if nn < 1e-6:
            total += 1.0
            continue
        d /= nn
        total += 1.0 - float(np.dot(d, drawn_dir[i]))   # 0 = perfect, 2 = opposite
    return total


def _pose_to_threejs(rvec, tvec):
    R, _ = cv2.Rodrigues(rvec)
    cam_pos = (-R.T @ tvec).flatten()
    forward = (R.T @ np.array([0.0, 0.0, 1.0])).flatten()
    up = (-R.T @ np.array([0.0, 1.0, 0.0])).flatten()
    look_at = cam_pos + forward
    d = lambda v: {"x": float(v[0]), "y": float(v[1]), "z": float(v[2])}
    return d(cam_pos), d(look_at), d(up)


def _backproject_to_z0(image_points, K, rvec, tvec):
    R, _ = cv2.Rodrigues(rvec)
    cam_pos = (-R.T @ tvec).flatten()
    K_inv = np.linalg.inv(K)
    out = []
    for (u, v) in image_points:
        ray = R.T @ (K_inv @ np.array([u, v, 1.0]))
        if abs(ray[2]) < 1e-9:
            out.append(None); continue
        s = -cam_pos[2] / ray[2]
        out.append(cam_pos + s * ray if s > 0 else None)
    return out


def solve_full(
    back_image_points: List[Tuple[float, float]],
    near_image_points: List[Tuple[float, float]],
    door_image_points: List[Tuple[float, float]],
    image_size_px: Tuple[int, int],
    garage_w: float,
    garage_l: float,
    garage_h: float,
    known_fov_deg: Optional[float] = None,
) -> dict:
    if len(back_image_points) != 4 or len(near_image_points) != 4:
        raise ValueError("need 4 back points and 4 near direction points")

    back_world = back_wall_world(garage_w, garage_h)
    near_world = near_wall_world(garage_w, garage_h, garage_l)
    back_img = np.array(back_image_points, dtype=np.float64)
    near_img = np.array(near_image_points, dtype=np.float64)

    drawn = near_img - back_img
    norms = np.linalg.norm(drawn, axis=1, keepdims=True)
    norms[norms < 1e-6] = 1.0
    drawn_dir = drawn / norms

    def solve_at(fov):
        K = intrinsics(image_size_px, fov)
        res = _ippe_back_wall(back_img, back_world, K)
        if res is None:
            return None
        rvec, tvec = res
        err = _edge_direction_error(K, rvec, tvec, back_world, near_world, drawn_dir)
        return (err, fov, rvec, tvec, K)

    if known_fov_deg and known_fov_deg > 0:
        chosen = solve_at(float(known_fov_deg))
        if chosen is None:
            raise ValueError("pose solve failed at the camera's FOV")
        fov_solved = False
    else:
        # Coarse search then refine — find the FOV whose receding edges match.
        best = None
        for fov in np.arange(25.0, 120.5, 1.0):
            cand = solve_at(fov)
            if cand and (best is None or cand[0] < best[0]):
                best = cand
        if best is None:
            raise ValueError("pose solve failed — check back-wall pins")
        f0 = best[1]
        for fov in np.arange(max(25.0, f0 - 2.0), min(120.0, f0 + 2.0) + 0.01, 0.25):
            cand = solve_at(fov)
            if cand and cand[0] < best[0]:
                best = cand
        chosen = best
        fov_solved = True

    _, fov, rvec, tvec, K = chosen
    cam_pos, look_at, up = _pose_to_threejs(rvec, tvec)

    bp, _ = cv2.projectPoints(back_world, rvec, tvec, K, np.zeros(4))
    residual_px = float(np.mean(np.linalg.norm(bp.reshape(-1, 2) - back_img, axis=1)))

    result = {
        "camera_position": cam_pos,
        "camera_look_at": look_at,
        "camera_up": up,
        "camera_fov_deg": float(fov),
        "fov_solved": fov_solved,
        "residual_px": residual_px,
    }

    if door_image_points and len(door_image_points) == 4:
        pts = _backproject_to_z0(door_image_points, K, rvec, tvec)
        if all(p is not None for p in pts):
            tl, tr, br, bl = pts
            width = (abs(tr[0] - tl[0]) + abs(br[0] - bl[0])) / 2.0
            height = (abs(tl[1] - bl[1]) + abs(tr[1] - br[1])) / 2.0
            result["door_opening_width"] = float(max(0.5, min(garage_w, width)))
            result["door_opening_height"] = float(max(0.5, min(garage_h, height)))
            result["door_center_x"] = float((tl[0] + tr[0] + bl[0] + br[0]) / 4.0)

    return result
