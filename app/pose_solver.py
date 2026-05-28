"""Camera-pose solver + door back-projection.

The calibration UI places 12 pins on the live image:

  * 8 garage-box corners — 4 on the near wall (z=length, where the camera is,
    appearing at the image edges) and 4 on the far/door wall (z=0, appearing
    nested inside). These have known 3D positions from garage W/L/H, so they
    drive the camera-pose solve (SQPNP, which handles the non-coplanar set).

  * 4 door-opening corners on the far wall (z=0). The door's real size is
    unknown, so these are NOT used in the pose solve. Instead, once we have the
    pose, we back-project each door pin as a camera ray and intersect it with
    the z=0 plane to recover the door's true width/height/centre.

World frame: +X right, +Y up, +Z from the door wall (z=0) toward the
camera-end wall (z=length).
"""

from typing import Dict, List, Optional, Tuple

import numpy as np
import cv2

# Canonical pin order — the frontend must send points in these orders.
ROOM_ORDER = [
    "near_tl", "near_tr", "near_br", "near_bl",   # z = length (outer/edges)
    "back_tl", "back_tr", "back_br", "back_bl",   # z = 0 (inner/door wall)
]
DOOR_ORDER = ["door_tl", "door_tr", "door_br", "door_bl"]   # z = 0


def room_corners_world(width: float, length: float, height: float) -> Dict[str, Tuple[float, float, float]]:
    w = width / 2.0
    return {
        "near_tl": (-w, height, length),
        "near_tr": ( w, height, length),
        "near_br": ( w, 0.0,    length),
        "near_bl": (-w, 0.0,    length),
        "back_tl": (-w, height, 0.0),
        "back_tr": ( w, height, 0.0),
        "back_br": ( w, 0.0,    0.0),
        "back_bl": (-w, 0.0,    0.0),
    }


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


def _solve(obj_pts: np.ndarray, img_pts: np.ndarray, K: np.ndarray):
    dist = np.zeros(4, dtype=np.float64)
    # SQPNP is robust for general (non-coplanar) point sets.
    ok, rvec, tvec = cv2.solvePnP(obj_pts, img_pts, K, dist, flags=cv2.SOLVEPNP_SQPNP)
    if not ok:
        ok, rvec, tvec = cv2.solvePnP(obj_pts, img_pts, K, dist, flags=cv2.SOLVEPNP_ITERATIVE)
    if not ok:
        raise ValueError("solvePnP failed — check pin placement")
    return rvec, tvec


def _pose_to_threejs(rvec: np.ndarray, tvec: np.ndarray) -> Tuple[dict, dict]:
    R, _ = cv2.Rodrigues(rvec)
    cam_pos = (-R.T @ tvec).flatten()
    forward_world = (R.T @ np.array([0.0, 0.0, 1.0])).flatten()
    look_at = cam_pos + forward_world
    return (
        {"x": float(cam_pos[0]), "y": float(cam_pos[1]), "z": float(cam_pos[2])},
        {"x": float(look_at[0]), "y": float(look_at[1]), "z": float(look_at[2])},
    )


def _backproject_to_z0(image_points: List[Tuple[float, float]], K: np.ndarray,
                       rvec: np.ndarray, tvec: np.ndarray) -> List[Optional[np.ndarray]]:
    R, _ = cv2.Rodrigues(rvec)
    cam_pos = (-R.T @ tvec).flatten()
    K_inv = np.linalg.inv(K)
    out: List[Optional[np.ndarray]] = []
    for (u, v) in image_points:
        ray_world = R.T @ (K_inv @ np.array([u, v, 1.0]))
        if abs(ray_world[2]) < 1e-9:
            out.append(None)
            continue
        s = -cam_pos[2] / ray_world[2]
        if s <= 0:  # plane is behind the camera for this ray
            out.append(None)
            continue
        out.append(cam_pos + s * ray_world)
    return out


def solve_full(
    room_image_points: List[Tuple[float, float]],
    door_image_points: List[Tuple[float, float]],
    image_size_px: Tuple[int, int],
    fov_deg: float,
    garage_w: float,
    garage_l: float,
    garage_h: float,
) -> dict:
    """Solve camera pose from the 8 room corners, then derive door dimensions
    from the 4 door corners via back-projection onto z=0.

    Returns camera_position, camera_look_at, door_opening_{width,height},
    door_center_x, and reprojection residual.
    """
    if len(room_image_points) != 8:
        raise ValueError("need 8 room image points")
    if fov_deg <= 0 or fov_deg >= 180:
        raise ValueError("fov_deg out of range")

    K = intrinsics(image_size_px, fov_deg)
    room_world_map = room_corners_world(garage_w, garage_l, garage_h)
    obj = np.array([room_world_map[k] for k in ROOM_ORDER], dtype=np.float64)
    img = np.array(room_image_points, dtype=np.float64)

    rvec, tvec = _solve(obj, img, K)
    camera_position, camera_look_at = _pose_to_threejs(rvec, tvec)

    # Residual on the room corners
    proj, _ = cv2.projectPoints(obj, rvec, tvec, K, np.zeros(4))
    residual_px = float(np.mean(np.linalg.norm(proj.reshape(-1, 2) - img, axis=1)))

    result = {
        "camera_position": camera_position,
        "camera_look_at": camera_look_at,
        "residual_px": residual_px,
    }

    # Door derivation (optional — only if 4 door points provided)
    if door_image_points and len(door_image_points) == 4:
        pts = _backproject_to_z0(door_image_points, K, rvec, tvec)
        if all(p is not None for p in pts):
            tl, tr, br, bl = pts
            width = (abs(tr[0] - tl[0]) + abs(br[0] - bl[0])) / 2.0
            height = (abs(tl[1] - bl[1]) + abs(tr[1] - br[1])) / 2.0
            center_x = float((tl[0] + tr[0] + bl[0] + br[0]) / 4.0)
            # Clamp to sane ranges so a bad pin doesn't produce a monster door.
            width = float(max(0.5, min(garage_w, width)))
            height = float(max(0.5, min(garage_h, height)))
            result["door_opening_width"] = width
            result["door_opening_height"] = height
            result["door_center_x"] = center_x

    return result
