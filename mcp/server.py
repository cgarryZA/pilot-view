"""Pilot View MCP server — native vehicle/garage tools for Claude.

A FastMCP server that wraps the local pilot-view FastAPI app
(http://127.0.0.1:8100) and exposes its read-only state and a small set of
actuator endpoints (lights, door, vehicle cycle).

Transport: Streamable HTTP (the new MCP transport spec). Listens on
127.0.0.1:8101 — one port above the pilot-view REST API itself so the two
processes can sit side-by-side.

Every tool carries a `Domain: vehicle.` line in its docstring so the chat tool
router can fan them in by domain.

Safety: write tools that actuate hardware (lights, door, vehicle cycle) require
an explicit `confirm=True` argument. Calling them with the default
`confirm=False` returns a structured refusal and the actuator stays put. This
keeps the LLM from opening the garage door because of a misread request.
"""

from __future__ import annotations

from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

# Host + port go in the FastMCP constructor — the .run() method only takes
# `transport` and `mount_path`. Naming this 127.0.0.1 keeps the server LAN-only;
# the Cloudflare Tunnel will pick it up in prod.
mcp = FastMCP("pilot-view", host="127.0.0.1", port=8101)

PILOT_BASE = "http://127.0.0.1:8100"


async def _fetch(
    path: str,
    method: str = "GET",
    params: dict[str, Any] | None = None,
    json_body: Any = None,
) -> dict[str, Any]:
    url = PILOT_BASE.rstrip("/") + "/" + path.lstrip("/")
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.request(method, url, params=params, json=json_body)
        except httpx.ConnectError:
            return {
                "ok": False,
                "error": "app_unreachable",
                "url": url,
                "hint": "pilot-view isn't running on 127.0.0.1:8100. Start it (e.g. `uvicorn app.main:app --port 8100`) and retry.",
            }
        except httpx.HTTPError as e:
            return {"ok": False, "error": "http_error", "message": str(e), "url": url}
    body: Any
    if "application/json" in resp.headers.get("content-type", ""):
        body = resp.json()
    else:
        body = resp.text[:5000]
    return {
        "ok": resp.status_code < 400,
        "status": resp.status_code,
        "url": url,
        "body": body,
    }


REFUSAL = {
    "ok": False,
    "error": "Refusing without confirm=True. Re-call with confirm=True if you really want to actuate the garage.",
}


# ---------- Read-only ----------


@mcp.tool()
async def get_pilot_state() -> dict[str, Any]:
    """Full Pilot View vehicle/garage state snapshot — door, lights, sensors,
    active vehicle, battery, recent events.

    Domain: vehicle.
    """
    return await _fetch("/api/state")


@mcp.tool()
async def get_pilot_vehicles() -> dict[str, Any]:
    """List every vehicle Pilot View knows about and which one is currently
    active.

    Domain: vehicle.
    """
    return await _fetch("/api/vehicles")


@mcp.tool()
async def get_pilot_environment() -> dict[str, Any]:
    """Garage environment readings — temperature, humidity, light level,
    motion sensor state.

    Domain: vehicle.
    """
    return await _fetch("/api/environment")


@mcp.tool()
async def get_pilot_diagnostics() -> dict[str, Any]:
    """Pilot View diagnostics — battery voltage, current errors, sensor
    health/last-seen times.

    Domain: vehicle.
    """
    return await _fetch("/api/diagnostics")


@mcp.tool()
async def get_pilot_calibration() -> dict[str, Any]:
    """Current calibration values — sensor offsets and per-vehicle calibration.

    Domain: vehicle.
    """
    return await _fetch("/api/calibration")


# ---------- Write (require confirm=True) ----------


@mcp.tool()
async def pilot_lights_on(confirm: bool = False) -> dict[str, Any]:
    """Turn the garage lights on.

    Domain: vehicle.

    Args:
        confirm: Must be True to actually fire the actuator. Defaults to False
            so a mis-routed request can't switch hardware.
    """
    if not confirm:
        return REFUSAL
    return await _fetch("/api/lights/on", method="POST")


@mcp.tool()
async def pilot_lights_off(confirm: bool = False) -> dict[str, Any]:
    """Turn the garage lights off.

    Domain: vehicle.

    Args:
        confirm: Must be True to actually fire the actuator. Defaults to False
            so a mis-routed request can't switch hardware.
    """
    if not confirm:
        return REFUSAL
    return await _fetch("/api/lights/off", method="POST")


@mcp.tool()
async def pilot_lights_toggle(confirm: bool = False) -> dict[str, Any]:
    """Toggle the garage lights (on <-> off).

    Domain: vehicle.

    Args:
        confirm: Must be True to actually fire the actuator. Defaults to False
            so a mis-routed request can't switch hardware.
    """
    if not confirm:
        return REFUSAL
    return await _fetch("/api/lights/toggle", method="POST")


@mcp.tool()
async def pilot_door_toggle(confirm: bool = False) -> dict[str, Any]:
    """Open or close the garage door (toggles current state).

    Domain: vehicle.

    Args:
        confirm: Must be True to actually fire the actuator. Defaults to False
            so a mis-routed request can't move the door.
    """
    if not confirm:
        return REFUSAL
    return await _fetch("/api/door/toggle", method="POST")


@mcp.tool()
async def pilot_cycle_vehicle(confirm: bool = False) -> dict[str, Any]:
    """Cycle the active vehicle to the next one in the known-vehicles list.

    Domain: vehicle.

    Args:
        confirm: Must be True to actually change active vehicle. Defaults to
            False so a mis-routed request can't reassign state.
    """
    if not confirm:
        return REFUSAL
    return await _fetch("/api/vehicles/cycle", method="POST")


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
