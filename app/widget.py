"""GET /widget — single-shot snapshot for the LifeOS workshop dashboard.

The workshop dashboard at christiangarry.com/workshop.html polls /widget on
every LifeOS app every ~30s and renders the result as a live tile. Shape is
deliberately tiny so the dashboard does zero heavy work.

Contract (all LifeOS apps follow this shape — see health/server/app/api/widget.py):

    {
      "app": "pilot-view",
      "headline": "Garage idle",
      "lines": ["Battery 12.6 V", "Doors closed", "Garage temp 18.2°C"],
      "status": "ok" | "warn" | "alert" | "stale",
      "links": [{ "label": "Dashboard", "href": "/" }],
      "updated_at": "2026-06-04T08:12:00Z"
    }

`status` drives the tile's accent colour:
  - ok    → green   (vehicle data fresh, nothing anomalous)
  - warn  → amber   (door open, battery 12.0–12.3 V, sensor stale > 1h)
  - alert → red     (battery < 12.0 V, critical sensor failed)
  - stale → grey    (no telemetry in > 24h)

Auth: same session gate as the rest of the API. The workshop dashboard is
same-origin under the Tailscale net so the session cookie rides along; external
callers must authenticate normally.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends

from app.auth import require_auth
from app.battery_monitor import battery_monitor
from app.door import door
from app.sensors import sensors
from app.vehicles import vehicles

router = APIRouter(tags=["widget"])

# Battery thresholds — lead-acid resting voltage interpretation.
BATTERY_ALERT_V = 12.0   # < this → potentially flat
BATTERY_WARN_V = 12.3    # 12.0–12.3 V → degraded

SENSOR_STALE_WARN = timedelta(hours=1)
TELEMETRY_STALE_GREY = timedelta(hours=24)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp; return None if missing/invalid.

    The various drivers emit `_now()` which produces a `+00:00` suffix that
    fromisoformat handles cleanly on Python 3.11+. Defensive against the
    occasional `Z` suffix just in case a future driver normalises differently.
    """
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@router.get(
    "/widget",
    summary="One-shot dashboard snapshot for the workshop tile",
    description=(
        "Returns the headline+lines+status payload the workshop dashboard "
        "renders as a tile. Reads the same in-memory state the UI's "
        "/api/state endpoint and /ws stream use — no new hardware reads, "
        "no DB hits."
    ),
)
def widget(_session: dict = Depends(require_auth)) -> dict[str, Any]:
    now = _now()

    # Door + environment + active-vehicle battery — same helpers the UI uses.
    door_snap = door.status_dict()
    env_snap = sensors.read()
    vstate = vehicles.state_dict()

    active = vstate.get("active") or {}
    active_id = vstate.get("active_id")
    mac = active.get("battery_monitor_mac")
    battery_snap: Optional[dict] = None
    if mac and active_id:
        reading = battery_monitor.read(active_id, mac)
        if reading and reading.get("available"):
            battery_snap = reading

    # ── derive freshness of the most recent piece of telemetry ─────────
    env_last = _parse_iso(env_snap.get("last_read")) if env_snap.get("available") else None
    bat_last = _parse_iso(battery_snap.get("last_read")) if battery_snap else None
    door_last = _parse_iso(door_snap.get("last_changed"))

    freshest: Optional[datetime] = None
    for ts in (env_last, bat_last, door_last):
        if ts and (freshest is None or ts > freshest):
            freshest = ts

    # ── status derivation ──────────────────────────────────────────────
    status = "ok"
    door_status = door_snap.get("status")

    # stale: no telemetry in > 24h. Treat door as long-lived state — only the
    # env/battery sensors are useful "is anything alive?" signals.
    sensor_last = env_last or bat_last
    if sensor_last is None or (now - sensor_last) > TELEMETRY_STALE_GREY:
        status = "stale"
    else:
        # alert wins over warn.
        if battery_snap is not None:
            v = battery_snap.get("voltage_v")
            if isinstance(v, (int, float)) and v < BATTERY_ALERT_V:
                status = "alert"
        if door_status == "fault":
            status = "alert"

        if status != "alert":
            if door_status in ("open", "partial"):
                status = "warn"
            if battery_snap is not None:
                v = battery_snap.get("voltage_v")
                if (
                    isinstance(v, (int, float))
                    and BATTERY_ALERT_V <= v < BATTERY_WARN_V
                ):
                    status = "warn"
            # Sensor stale > 1h but < 24h
            if env_last is not None and (now - env_last) > SENSOR_STALE_WARN:
                status = "warn"
            if (
                battery_snap is not None
                and bat_last is not None
                and (now - bat_last) > SENSOR_STALE_WARN
            ):
                status = "warn"

    # ── headline ───────────────────────────────────────────────────────
    headline_map = {
        "closed": "Garage idle",
        "open": "Garage door open",
        "partial": "Garage door moving",
        "fault": "Garage door fault",
    }
    headline = headline_map.get(door_status or "", "Garage")
    if status == "stale":
        headline = "Garage offline"
    elif status == "alert" and battery_snap is not None:
        v = battery_snap.get("voltage_v")
        if isinstance(v, (int, float)) and v < BATTERY_ALERT_V:
            headline = "Battery flat"

    # ── lines ──────────────────────────────────────────────────────────
    lines: list[str] = []

    if battery_snap is not None:
        v = battery_snap.get("voltage_v")
        if isinstance(v, (int, float)):
            label = active.get("name") or active_id or "Battery"
            charging = " (charging)" if battery_snap.get("charging") else ""
            lines.append(f"{label} battery {v:.1f} V{charging}")

    if door_status:
        door_line = {
            "closed": "Doors closed",
            "open": "Door open",
            "partial": "Door moving",
            "fault": "Door sensor fault",
        }.get(door_status, f"Door {door_status}")
        lines.append(door_line)

    if env_snap.get("available"):
        temp = env_snap.get("temperature_c")
        humid = env_snap.get("humidity_pct")
        if isinstance(temp, (int, float)) and isinstance(humid, (int, float)):
            lines.append(f"Garage {temp:.1f}°C · {humid:.0f}% RH")
        elif isinstance(temp, (int, float)):
            lines.append(f"Garage {temp:.1f}°C")

    if not lines:
        lines.append("No telemetry")

    return {
        "app": "pilot-view",
        "headline": headline,
        "lines": lines,
        "status": status,
        "links": [{"label": "Dashboard", "href": "/"}],
        "updated_at": (freshest or now).isoformat().replace("+00:00", "Z"),
    }
