import asyncio
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import automations, calibration, diagnostics
from app.battery_monitor import battery_monitor
from app.door import door
from app.lights import lights
from app.sensors import sensors
from app.sources import make_source
from app.vehicles import vehicles

automations.install()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "static"

app = FastAPI(title="Pilot View")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

source = make_source()


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/state")
def state():
    return _ws_payload()


@app.get("/api/calibration")
def get_calibration():
    return calibration.load()


@app.put("/api/calibration")
async def put_calibration(updates: dict):
    if not isinstance(updates, dict):
        raise HTTPException(400, "expected JSON object")
    return calibration.save(updates)


@app.post("/api/calibration/reset")
def reset_calibration():
    return calibration.reset()


@app.get("/api/door")
def get_door():
    return door.status_dict()


@app.post("/api/door/open")
def door_open():
    return door.trigger_open()


@app.post("/api/door/close")
def door_close():
    return door.trigger_close()


@app.post("/api/door/toggle")
def door_toggle():
    return door.trigger_toggle()


@app.get("/api/lights")
def get_lights():
    return lights.status_dict()


@app.post("/api/lights/on")
def lights_on():
    return lights.trigger_on()


@app.post("/api/lights/off")
def lights_off():
    return lights.trigger_off()


@app.post("/api/lights/toggle")
def lights_toggle():
    return lights.trigger_toggle()


@app.get("/api/environment")
def get_environment():
    return sensors.read()


@app.get("/api/diagnostics")
def get_diagnostics():
    return diagnostics.snapshot()


@app.get("/api/vehicles")
def get_vehicles():
    return vehicles.state_dict()


@app.post("/api/vehicles/cycle")
def cycle_vehicle():
    return vehicles.cycle_active()


@app.post("/api/vehicles/active")
async def set_active_vehicle(payload: dict):
    if not isinstance(payload, dict) or "id" not in payload:
        raise HTTPException(400, "expected {'id': '<vehicle_id>'}")
    return vehicles.set_active(payload["id"])


def _ws_payload() -> dict:
    payload = source.state()
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


@app.websocket("/ws")
async def ws(socket: WebSocket):
    await socket.accept()
    try:
        while True:
            await socket.send_json(_ws_payload())
            await asyncio.sleep(1 / 15)
    except WebSocketDisconnect:
        return
