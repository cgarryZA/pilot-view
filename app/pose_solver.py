"""Camera-pose solver — given 4 pins on a 2D image, recover the camera that
took the image.

We use the corners of the garage door opening as the reference rectangle:

    top-left  ──────── top-right     world frame:
       │                  │            +X = right
       │                  │            +Y = up
    bottom-left ──── bottom-right      +Z = into garage (away from entrance)

These four points are always coplanar (they lie on the entrance plane z=0) and
are reliably visible in any interior shot of the garage. OpenCV's IPPE solver
handles coplanar 4-point PnP cleanly.

We assume the FOV is known (passed as an input — adjusted manually by the user
or estimated separately). solvePnP needs intrinsics; with only 4 points we
can't recover intrinsics AND extrinsics jointly without ambiguity.

The result is a Three.js-friendly (camera_position, camera_look_at) pair.
"""

from typing import List, Tuple

import numpy as np
import cv2


def door_opening_world_corners(door_w: float, door_h: float) -> List[Tuple[float, float, float]]:
    """Returns the 4 corners of the entrance door opening in world coordinates.

    Order matches what the UI labels: bottom-left, bottom-right, top-right, top-left.
    """
    half = door_w / 2
    return [
        (-half, 0.0, 0.0),       # bottom-left
        ( half, 0.0, 0.0),       # bottom-right
        ( half, door_h, 0.0),    # top-right
        (-half, door_h, 0.0),    # top-left
    ]


def _intrinsics(image_size_px: Tuple[int, int], fov_deg: float) -> np.ndarray:
    """Build a 3x3 K assuming square pixels and principal point at the centre."""
    w, h = image_size_px
    fov_rad = np.deg2rad(fov_deg)
    # Treat fov_deg as the vertical FOV. Focal length in pixels:
    fy = (h / 2.0) / np.tan(fov_rad / 2.0)
    fx = fy  # square pixels
    cx = w / 2.0
    cy = h / 2.0
    return np.array(
        [[fx, 0.0, cx],
         [0.0, fy, cy],
         [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )


def solve_camera_pose(
    image_points: List[Tuple[float, float]],
    image_size_px: Tuple[int, int],
    world_points: List[Tuple[float, float, float]],
    fov_deg: float,
) -> dict:
    """Solve for the camera pose from 4 image-point/world-point correspondences.

    Returns:
        {
          "camera_position": {"x", "y", "z"},
          "camera_look_at":  {"x", "y", "z"},
          "residual_px": float,    # mean reprojection error
        }

    Raises ValueError if the solver fails or the geometry is degenerate.
    """
    if len(image_points) != 4 or len(world_points) != 4:
        raise ValueError("need exactly 4 image points and 4 world points")
    if fov_deg <= 0 or fov_deg >= 180:
        raise ValueError("fov_deg must be in (0, 180)")
    w, h = image_size_px
    if w <= 0 or h <= 0:
        raise ValueError("image_size_px must be positive")

    obj_pts = np.array(world_points, dtype=np.float64)
    img_pts = np.array(image_points, dtype=np.float64)
    K = _intrinsics(image_size_px, fov_deg)
    dist = np.zeros(4, dtype=np.float64)

    # IPPE is the right algorithm for coplanar 4-point PnP. It returns the best
    # of two ambiguous solutions internally.
    ok, rvec, tvec = cv2.solvePnP(
        objectPoints=obj_pts,
        imagePoints=img_pts,
        cameraMatrix=K,
        distCoeffs=dist,
        flags=cv2.SOLVEPNP_IPPE,
    )
    if not ok:
        # IPPE can refuse if points are degenerate. Try the iterative solver.
        ok, rvec, tvec = cv2.solvePnP(
            obj_pts, img_pts, K, dist, flags=cv2.SOLVEPNP_ITERATIVE
        )
        if not ok:
            raise ValueError("solvePnP failed — image points may be degenerate")

    R, _ = cv2.Rodrigues(rvec)

    # Camera position in world frame: solving t_world from
    #   P_cam = R · P_world + t  →  if P_world = camera, P_cam = 0  →  P_world = -R.T · t
    cam_pos = (-R.T @ tvec).flatten()

    # Camera's forward direction in OpenCV's camera frame is +Z. Transform to
    # world frame:
    forward_world = (R.T @ np.array([0.0, 0.0, 1.0])).flatten()
    look_at = cam_pos + forward_world  # 1 m along the view direction

    # Reprojection residual — mean pixel error across the 4 points. Useful for
    # the UI to show whether the user's pin placement made sense.
    proj, _ = cv2.projectPoints(obj_pts, rvec, tvec, K, dist)
    proj = proj.reshape(-1, 2)
    residual_px = float(np.mean(np.linalg.norm(proj - img_pts, axis=1)))

    return {
        "camera_position": {
            "x": float(cam_pos[0]),
            "y": float(cam_pos[1]),
            "z": float(cam_pos[2]),
        },
        "camera_look_at": {
            "x": float(look_at[0]),
            "y": float(look_at[1]),
            "z": float(look_at[2]),
        },
        "residual_px": residual_px,
    }
