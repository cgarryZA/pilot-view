import asyncio
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import auth, automations, calibration, diagnostics, pose_solver
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

@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


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
        room = payload["room_points"]
        door = payload.get("door_points") or []
        size = payload["image_size"]
        fov = float(payload.get("fov_deg") or 50.0)
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(400, f"invalid payload: {exc}")

    if not isinstance(room, list) or len(room) != 8 or any(len(p) != 2 for p in room):
        raise HTTPException(400, "room_points must be a list of 8 [u, v] pairs")

    cal = calibration.load()
    garage = cal.get("garage", {})
    image_size = (int(size["width"]), int(size["height"]))
    room_pts = [(float(u), float(v)) for u, v in room]
    door_pts = [(float(u), float(v)) for u, v in door] if len(door) == 4 else []

    try:
        result = pose_solver.solve_full(
            room_image_points=room_pts,
            door_image_points=door_pts,
            image_size_px=image_size,
            fov_deg=fov,
            garage_w=float(garage.get("width", 3.0)),
            garage_l=float(garage.get("length", 5.8)),
            garage_h=float(garage.get("height", 2.3)),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    updates = {
        "live_view": {
            "camera_position": result["camera_position"],
            "camera_look_at":  result["camera_look_at"],
            "camera_fov_deg":  fov,
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
