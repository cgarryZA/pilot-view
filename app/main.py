import asyncio
import re
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import auth, automations, calibration, depth, diagnostics, pose_solver, terminal
from app.auth import require_auth, current_session
from app.battery_monitor import battery_monitor
from app.door import door
from app.lights import lights
from app.passkeys import SESSION_COOKIE_NAME, sessions
from app.sensors import sensors
from app.sources import SOURCE_NAMES, source_manager
from app.vehicles import vehicles

automations.install()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "static"

app = FastAPI(title="Pilot View")
app.include_router(auth.router)


@app.middleware("http")
async def add_cache_control(request, call_next):
    """Static assets must revalidate every load. ETag/Last-Modified are still set
    by StaticFiles so 304s remain cheap — we just force the browser to ask."""
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/static/") or path == "/":
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ─── Open endpoints (no auth) ───────────────────────────────────────────

_MOBILE_UA = re.compile(r"Mobi|Android|iPhone|iPad|iPod|IEMobile|BlackBerry|Opera Mini", re.I)
DESKTOP_HTML = STATIC_DIR / "index.html"
MOBILE_HTML = STATIC_DIR / "mobile.html"


@app.get("/")
def index(request: Request):
    """Serve the mobile-first UI to phones and the desktop UI to PCs.
    Override with ?desktop or ?mobile; /m and /d force a specific one."""
    q = request.query_params
    if "desktop" in q:
        return FileResponse(DESKTOP_HTML)
    if "mobile" in q:
        return FileResponse(MOBILE_HTML)
    is_mobile = bool(_MOBILE_UA.search(request.headers.get("user-agent", "")))
    return FileResponse(MOBILE_HTML if is_mobile else DESKTOP_HTML)


@app.get("/depth")
def depth_page():
    return FileResponse(STATIC_DIR / "depth.html")


def _depth_frame():
    """Prefer the active source's depth (orbbec, one shared pipeline); fall back
    to the standalone depth camera (e.g. when running source=synthetic on dev)."""
    d = source_manager.get_depth()
    if d is not None:
        return d
    return depth.depth_camera.get_depth()


@app.get("/api/depth/cloud")
def depth_cloud():
    d = _depth_frame()
    data = depth.cloud_bytes(*d) if d else b""
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"X-Point-Count": str(len(data) // 12), "Cache-Control": "no-store"},
    )


@app.get("/api/depth/segments")
def depth_segments():
    d = _depth_frame()
    data = depth.segmented_bytes(*d) if d else b""
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"X-Point-Count": str(len(data) // 24), "Cache-Control": "no-store"},
    )


def _expected_extent():
    """(length, width, height) of the active vehicle, or None."""
    active = vehicles.state_dict().get("active") or {}
    ext = active.get("extent")
    if ext and all(k in ext for k in ("length", "width", "height")):
        return (float(ext["length"]), float(ext["width"]), float(ext["height"]))
    return None


@app.get("/api/depth/detect.png")
def depth_detect_png():
    d = _depth_frame()
    data = depth.detect_debug_png(*d, expect=_expected_extent()) if d else b""
    return Response(content=data, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.get("/api/depth/status")
def depth_status():
    src = source_manager.get_depth()
    return {"source_has_depth": src is not None, **depth.depth_camera.status()}


@app.get("/m")
def index_mobile():
    return FileResponse(MOBILE_HTML)


@app.get("/d")
def index_desktop():
    return FileResponse(DESKTOP_HTML)


@app.get("/terminal")
def terminal_page():
    # Self-contained admin shell (vendored xterm.js) — the only way to drive the
    # Pi from a phone once it's its own access point. No auth wall here; the
    # WebSocket below enforces the same gate as /ws.
    return FileResponse(STATIC_DIR / "terminal.html")


# ─── Protected endpoints — every API call requires a valid session ──────

@app.get("/api/state")
def state(_=Depends(require_auth)):
    return _ws_payload()


@app.get("/api/calibration")
def get_calibration(_=Depends(require_auth)):
    return calibration.load()


@app.put("/api/calibration")
async def put_calibration(updates: dict, _=Depends(require_auth)):
    if not isinstance(updates, dict):
        raise HTTPException(400, "expected JSON object")
    return calibration.save(updates)


@app.post("/api/calibration/reset")
def reset_calibration(_=Depends(require_auth)):
    return calibration.reset()


@app.post("/api/calibration/solve_pose")
async def solve_pose(payload: dict, _=Depends(require_auth)):
    """Solve camera pose from 8 room-corner pins + derive door from 4 door pins.

    Input:
      {
        "room_points": [[u,v] x8],   // near_tl,tr,br,bl, back_tl,tr,br,bl
        "door_points": [[u,v] x4],   // door_tl,tr,br,bl  (optional)
        "image_size":  {"width": int, "height": int},
        "fov_deg":     float
      }
    """
    if not isinstance(payload, dict):
        raise HTTPException(400, "expected JSON object")
    try:
        back = payload["back_points"]
        near = payload["near_points"]
        door = payload.get("door_points") or []
        size = payload["image_size"]
        known_fov = payload.get("known_fov_deg")  # supplied for real-camera intrinsics
        apply = bool(payload.get("apply", True))
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(400, f"invalid payload: {exc}")

    for name, arr in (("back_points", back), ("near_points", near)):
        if not isinstance(arr, list) or len(arr) != 4 or any(len(p) != 2 for p in arr):
            raise HTTPException(400, f"{name} must be a list of 4 [u, v] pairs")

    cal = calibration.load()
    garage = cal.get("garage", {})
    image_size = (int(size["width"]), int(size["height"]))
    back_pts = [(float(u), float(v)) for u, v in back]
    near_pts = [(float(u), float(v)) for u, v in near]
    door_pts = [(float(u), float(v)) for u, v in door] if len(door) == 4 else []

    try:
        result = pose_solver.solve_full(
            back_image_points=back_pts,
            near_image_points=near_pts,
            door_image_points=door_pts,
            image_size_px=image_size,
            garage_w=float(garage.get("width", 3.0)),
            garage_l=float(garage.get("length", 5.8)),
            garage_h=float(garage.get("height", 2.3)),
            known_fov_deg=float(known_fov) if known_fov else None,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    updates = {
        "live_view": {
            "camera_position": result["camera_position"],
            "camera_look_at":  result["camera_look_at"],
            "camera_up":       result["camera_up"],
            "camera_fov_deg":  result["camera_fov_deg"],
        },
    }
    # Apply derived door dimensions if back-projection succeeded.
    garage_updates = {}
    if "door_opening_width" in result:
        garage_updates["door_opening_width"] = result["door_opening_width"]
        garage_updates["door_opening_height"] = result["door_opening_height"]
        garage_updates["door_center_x"] = result["door_center_x"]
    if garage_updates:
        updates["garage"] = garage_updates

    # Preview solves (apply=false) return the pose without persisting, so the
    # UI can show the live box alignment as the user drags pins.
    if apply:
        calibration.save(updates)
    return result


@app.post("/api/calibration/auto_pose")
def auto_pose(_=Depends(require_auth)):
    """Auto-calibrate the camera pose straight from depth — no manual pinning.
    Fits the floor (height + tilt) and the facing wall (yaw + distance)."""
    d = _depth_frame()
    if not d:
        raise HTTPException(400, "no depth frame yet — is the camera streaming?")
    cam = source_manager.state().get("camera", {})
    fov = cam.get("fov_deg") or 55.0
    result = depth.auto_pose_from_depth(*d, fov_deg=float(fov))
    if result is None:
        raise HTTPException(400, "could not fit a floor + facing wall from the depth cloud")
    updates = {
        "live_view": {
            "camera_position": result["camera_position"],
            "camera_look_at": result["camera_look_at"],
            "camera_up": result["camera_up"],
            "camera_fov_deg": result["camera_fov_deg"],
        },
    }
    # Auto-measured garage envelope (deep-merged, so door dims are preserved).
    # Editable afterward in the Garage tab if a wall wasn't fully visible.
    if result.get("garage"):
        updates["garage"] = result["garage"]
    calibration.save(updates)
    return result


@app.get("/api/door")
def get_door(_=Depends(require_auth)):
    return door.status_dict()


@app.post("/api/door/open")
def door_open(_=Depends(require_auth)):
    return door.trigger_open()


@app.post("/api/door/close")
def door_close(_=Depends(require_auth)):
    return door.trigger_close()


@app.post("/api/door/toggle")
def door_toggle(_=Depends(require_auth)):
    return door.trigger_toggle()


@app.get("/api/lights")
def get_lights(_=Depends(require_auth)):
    return lights.status_dict()


@app.post("/api/lights/on")
def lights_on(_=Depends(require_auth)):
    return lights.trigger_on()


@app.post("/api/lights/off")
def lights_off(_=Depends(require_auth)):
    return lights.trigger_off()


@app.post("/api/lights/toggle")
def lights_toggle(_=Depends(require_auth)):
    return lights.trigger_toggle()


@app.get("/api/environment")
def get_environment(_=Depends(require_auth)):
    return sensors.read()


@app.get("/api/diagnostics")
def get_diagnostics(_=Depends(require_auth)):
    return diagnostics.snapshot()


@app.get("/api/vehicles")
def get_vehicles(_=Depends(require_auth)):
    return vehicles.state_dict()


@app.post("/api/vehicles/cycle")
def cycle_vehicle(_=Depends(require_auth)):
    return vehicles.cycle_active()


@app.post("/api/vehicles/active")
async def set_active_vehicle(payload: dict, _=Depends(require_auth)):
    if not isinstance(payload, dict) or "id" not in payload:
        raise HTTPException(400, "expected {'id': '<vehicle_id>'}")
    return vehicles.set_active(payload["id"])


@app.get("/api/camera/stream")
async def camera_stream(_=Depends(require_auth)):
    """MJPEG stream of the current source's latest frames (orbbec only)."""
    if source_manager.get_jpeg() is None:
        raise HTTPException(404, "no video stream from current source")

    async def gen():
        boundary = b"--frame"
        while True:
            jpeg = source_manager.get_jpeg()
            if jpeg:
                yield boundary + b"\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
            await asyncio.sleep(1 / 20)

    return StreamingResponse(
        gen(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/source")
def get_source(_=Depends(require_auth)):
    return {"current": source_manager.name, "available": list(SOURCE_NAMES)}


@app.post("/api/source")
async def set_source(payload: dict, _=Depends(require_auth)):
    name = (payload or {}).get("name")
    if not isinstance(name, str):
        raise HTTPException(400, "expected {'name': '<source>'}")
    try:
        new_name = source_manager.switch(name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"failed to switch source: {exc}")
    return {"current": new_name, "available": list(SOURCE_NAMES)}


def _ws_payload() -> dict:
    payload = source_manager.state()
    payload["door"] = door.status_dict()
    payload["lights"] = lights.status_dict()
    payload["environment"] = sensors.read()

    vstate = vehicles.state_dict()
    payload["vehicles"] = vstate

    active = vstate.get("active") or {}
    mac = active.get("battery_monitor_mac")
    if mac and vstate.get("active_id"):
        reading = battery_monitor.read(vstate["active_id"], mac)
        if reading and reading.get("available"):
            reading["vehicle_id"] = vstate["active_id"]
            payload["battery"] = reading
        else:
            payload["battery"] = None
    else:
        payload["battery"] = None

    return payload


# WebSocket auth is manual — FastAPI dependencies can attach to WS endpoints but
# can't easily inject a response. We check the session cookie before accepting.
@app.websocket("/ws")
async def ws(socket: WebSocket):
    import os as _os
    auth_disabled = _os.getenv("PILOT_VIEW_AUTH", "enabled").strip().lower() == "disabled"
    if not auth_disabled:
        sid = socket.cookies.get(SESSION_COOKIE_NAME)
        if not sid or not sessions.get(sid):
            await socket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
    await socket.accept()
    try:
        while True:
            await socket.send_json(_ws_payload())
            await asyncio.sleep(1 / 15)
    except WebSocketDisconnect:
        return


@app.websocket("/api/terminal/ws")
async def terminal_ws(socket: WebSocket):
    await terminal.terminal_endpoint(socket)
