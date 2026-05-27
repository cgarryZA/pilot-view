import asyncio
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.sources import make_source

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
    return source.state()


@app.websocket("/ws")
async def ws(socket: WebSocket):
    await socket.accept()
    try:
        while True:
            await socket.send_json(source.state())
            await asyncio.sleep(1 / 15)  # ~15 Hz state updates
    except WebSocketDisconnect:
        return
