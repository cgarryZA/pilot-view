"""Camera-pose + FOV solver from a known cuboid and its receding edges.

Node system (matches the UI):
  * BLUE  — the 4 exact corners of the back/door wall (a known W×H rectangle on z=0).
  * WHITE — direction-only nodes. The line BLUE→WHITE is the image direction of the
            receding edge (the wall↔floor / wall↔ceiling seam). The true near
            corner is off-screen at  blue_world + (0,0,length); only the WHITE
            node's *direction* from its blue corner is meaningful.
  * YELLOW — the 4 exact corners of the garage-door opening (used for door dims).

How pose + FOV are found: a back-wall rectangle alone is an ill-conditioned PnP
target for a down-the-corridor view — the wall is nearly fronto-parallel and
distant, so tiny pin errors throw the focal length (FOV) and pose wildly. IPPE
will happily fit *any* FOV to a planar quad, which is exactly why solving FOV
from the back wall by itself produces garbage.

The fix: solve pose AND focal length together in one least-squares problem that
uses BOTH constraints the user actually drew —
  (a) the 4 BLUE corners must reproject onto the known back-wall rectangle, and
  (b) each receding edge, when projected, must point along the user's BLUE→WHITE
      direction (the room's +Z axis as seen in the image).
Constraint (b) is what pins the FOV: the only focal length that makes all four
parallel +Z edges appear to recede at the drawn angles is the true one. We
initialise from an IPPE pose on the back wall (camera-above-floor branch) at a
~60° FOV guess and let scipy.optimize.least_squares refine [rvec, tvec, f].

If the caller supplies a known FOV (e.g. read from the real Gemini 2's
intrinsics), we fix the focal length and only refine the 6-DoF pose.

Returns a Three.js-ready (position, look_at, up). up is derived from the solved
rotation so the OpenCV(+Y-down) → Three.js(+Y-up) conversion is exact.
"""

from typing import Dict, List, Optional, Tuple

import numpy as np
import cv2
from scipy.optimize import least_squares

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


def recede_probe_world(width: float, height: float, step: float = 0.25) -> np.ndarray:
    """A tiny +Z step from each BLUE back-wall corner.

    A 3D line projects to a straight 2D line, so the receding edge's image
    direction is *constant* along its whole length — sampling 25 cm in front of
    the back corner gives the exact same direction as the full (off-screen) near
    corner, but the probe point is always in front of the camera. That keeps the
    direction (and its sign) well-defined even when the camera sits right at the
    near wall, where projecting the true near corner would be numerically
    garbage (it lies at/behind the camera)."""
    w = width / 2.0
    return np.array([
        [-w, height, step],
        [ w, height, step],
        [ w, 0.0,    step],
        [-w, 0.0,    step],
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


def _f_from_fov(h: float, fov_deg: float) -> float:
    return (h / 2.0) / np.tan(np.deg2rad(fov_deg) / 2.0)


def _fov_from_f(h: float, f: float) -> float:
    return float(np.rad2deg(2.0 * np.arctan((h / 2.0) / f)))


def _K_from_f(f: float, cx: float, cy: float) -> np.ndarray:
    return np.array([[f, 0.0, cx], [0.0, f, cy], [0.0, 0.0, 1.0]], dtype=np.float64)


def _direction_error(K, rvec, tvec, back_world, near_world, drawn_dir) -> float:
    """Mean (1 - cos θ) between each projected +Z receding edge and the user's
    BLUE→WHITE direction. 0 = perfect, 2 = pointing the opposite way."""
    bp, _ = cv2.projectPoints(back_world, rvec, tvec, K, np.zeros(4))
    npj, _ = cv2.projectPoints(near_world, rvec, tvec, K, np.zeros(4))
    bp = bp.reshape(-1, 2)
    npj = npj.reshape(-1, 2)
    total = 0.0
    for i in range(4):
        d = npj[i] - bp[i]
        nn = np.linalg.norm(d)
        if nn < 1e-9:
            total += 1.0
            continue
        d /= nn
        total += 1.0 - float(np.dot(d, drawn_dir[i]))
    return total / 4.0


def _ippe_back_wall(back_img, back_world, near_world, drawn_dir, K):
    """Pose from the back-wall rectangle; choose the mirror branch whose receding
    edges point the way the user drew (with camera-above-floor as a tiebreak).

    Used only to *initialise* the joint optimiser — IPPE on a planar quad is not
    trusted for the final answer.
    """
    n, rvecs, tvecs, _ = cv2.solvePnPGeneric(
        back_world, back_img, K, np.zeros(4), flags=cv2.SOLVEPNP_IPPE
    )
    if n < 1:
        return None
    best = None
    for i in range(n):
        R, _ = cv2.Rodrigues(rvecs[i])
        cam = (-R.T @ tvecs[i]).flatten()
        dir_err = _direction_error(K, rvecs[i], tvecs[i], back_world, near_world, drawn_dir)
        # Lower is better: direction agreement dominates, tiny nudge for cam-above-floor.
        score = dir_err - (0.05 if cam[1] > 0 else 0.0)
        if best is None or score < best[0]:
            best = (score, rvecs[i], tvecs[i])
    return best[1], best[2]


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


def _make_residuals(back_world, near_world, back_img, drawn_dir,
                    cx, cy, diag, fixed_f):
    """Build the residual function for least_squares.

    Residuals (all in pixel-equivalent units so they're comparably weighted):
      * 8 — back-wall corner reprojection error (x,y for 4 BLUE corners).
      * 8 — receding-edge direction error: the projected +Z edge at each corner
            must align with the user's BLUE→WHITE unit direction. We use the
            difference of the two unit vectors (magnitude ≈ angular error in rad)
            scaled by the image diagonal so a 1° miss ≈ diag·(π/180) px.
    """
    rvec0_shape = (3, 1)

    def residuals(params):
        rvec = np.asarray(params[0:3], dtype=np.float64).reshape(rvec0_shape)
        tvec = np.asarray(params[3:6], dtype=np.float64).reshape(rvec0_shape)
        f = fixed_f if fixed_f is not None else float(params[6])
        K = _K_from_f(f, cx, cy)

        bp, _ = cv2.projectPoints(back_world, rvec, tvec, K, np.zeros(4))
        npj, _ = cv2.projectPoints(near_world, rvec, tvec, K, np.zeros(4))
        bp = bp.reshape(-1, 2)
        npj = npj.reshape(-1, 2)

        res = list((bp - back_img).ravel())
        for i in range(4):
            d = npj[i] - bp[i]
            nn = np.linalg.norm(d)
            if nn < 1e-9:
                res.extend([diag, diag])
                continue
            d = d / nn
            res.extend(((d - drawn_dir[i]) * diag).tolist())
        return np.asarray(res, dtype=np.float64)

    return residuals


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

    w_px, h_px = image_size_px
    cx, cy = w_px / 2.0, h_px / 2.0
    diag = float(np.hypot(w_px, h_px))

    back_world = back_wall_world(garage_w, garage_h)
    # Direction constraint uses a small +Z probe (constant image direction along
    # the receding edge), not the off-screen near corner — see recede_probe_world.
    near_world = recede_probe_world(garage_w, garage_h)
    back_img = np.array(back_image_points, dtype=np.float64)
    near_img = np.array(near_image_points, dtype=np.float64)

    drawn = near_img - back_img
    norms = np.linalg.norm(drawn, axis=1, keepdims=True)
    norms[norms < 1e-6] = 1.0
    drawn_dir = drawn / norms

    have_fov = bool(known_fov_deg and known_fov_deg > 0)

    def solve_pose_at_fov(fov_deg):
        """Pose-only refine at a FIXED FOV — a well-conditioned 6-DoF problem.
        Returns (rvec, tvec, dir_err) where dir_err is the leftover receding-edge
        mismatch (this is the only term that carries FOV information)."""
        K = intrinsics(image_size_px, fov_deg)
        init = _ippe_back_wall(back_img, back_world, near_world, drawn_dir, K)
        if init is None:
            return None
        rvec0, tvec0 = init
        x0 = np.concatenate([np.ravel(rvec0), np.ravel(tvec0)])
        if not np.all(np.isfinite(x0)):
            return None  # IPPE degenerate at this FOV — skip this candidate
        residuals = _make_residuals(
            back_world, near_world, back_img, drawn_dir, cx, cy, diag,
            fixed_f=_f_from_fov(h_px, fov_deg),
        )
        try:
            sol = least_squares(residuals, x0, method="lm", max_nfev=300)
        except (ValueError, np.linalg.LinAlgError):
            return None
        rvec = sol.x[0:3].reshape(3, 1)
        tvec = sol.x[3:6].reshape(3, 1)
        dir_err = _direction_error(K, rvec, tvec, back_world, near_world, drawn_dir)
        if not np.isfinite(dir_err):
            return None
        return rvec, tvec, dir_err

    if have_fov:
        # Real camera FOV from intrinsics — trust it, only refine pose.
        out = solve_pose_at_fov(float(known_fov_deg))
        if out is None:
            raise ValueError("pose solve failed at the camera's FOV")
        rvec, tvec, _ = out
        fov = float(known_fov_deg)
        fov_solved = False
    else:
        # No intrinsics: find the FOV whose receding edges best match the drawn
        # directions. A 1-D argmin over well-conditioned pose solves — stable,
        # never runs away (unlike a free joint optimisation).
        best = None  # (dir_err, fov, rvec, tvec)
        for fov_deg in np.arange(25.0, 120.5, 1.0):
            out = solve_pose_at_fov(fov_deg)
            if out is None:
                continue
            if best is None or out[2] < best[0]:
                best = (out[2], fov_deg, out[0], out[1])
        if best is None:
            raise ValueError("pose solve failed — check back-wall pins")
        f0 = best[1]
        for fov_deg in np.arange(max(25.0, f0 - 2.0), min(120.0, f0 + 2.0) + 0.01, 0.25):
            out = solve_pose_at_fov(fov_deg)
            if out is not None and out[2] < best[0]:
                best = (out[2], fov_deg, out[0], out[1])
        _, fov, rvec, tvec = best
        fov_solved = True

    K = intrinsics(image_size_px, fov)

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
