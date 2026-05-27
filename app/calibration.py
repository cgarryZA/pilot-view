import json
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
CONFIG_FILE = CONFIG_DIR / "calibration.json"

def _vehicle_defaults(
    name: str,
    model_url: str,
    extent: dict,
    mirror_x: bool = False,
    beacon_id: str = "",
    battery_monitor_mac: str = "",
    mesh_variants: list[str] | None = None,
) -> dict:
    return {
        "name": name,
        "model_url": model_url,
        "extent": extent,
        "model_offset": {"x": 0.0, "y": 0.0, "z": 0.0},
        "model_yaw_deg": 0.0,
        "model_scale": 1.0,
        "model_mirror_x": mirror_x,
        "beacon_id": beacon_id,
        "battery_monitor_mac": battery_monitor_mac,
        # Decimated mesh variants — empty means the canonical model_url is
        # locked in and the quality cycler shouldn't offer alternatives.
        "mesh_variants": mesh_variants or [],
    }


DEFAULTS: dict[str, Any] = {
    "garage": {"width": 3.0, "length": 5.8, "height": 2.3},
    "vehicles": {
        "active_id": "lambo",
        "registry": {
            "lambo": _vehicle_defaults(
                name="Lamborghini Gallardo",
                # Canonical Lambo = the 70% decimated variant.
                model_url="/static/assets/models/gallardo.glb",
                extent={"length": 4.30, "width": 1.90, "height": 1.16},
                battery_monitor_mac="synthetic-lambo",
                mesh_variants=[],
            ),
            "mazda": _vehicle_defaults(
                name="Mazda",
                # Canonical Mazda = the 3% decimated variant (3% looked fine for this model).
                model_url="/static/assets/models/mazda.glb",
                extent={"length": 4.07, "width": 1.70, "height": 1.51},
                # No alternatives — Mazda is locked in.
                mesh_variants=[],
            ),
        },
    },
    "live_view": {
        "camera_position": {"x": 0.0, "y": 1.45, "z": 5.6},
        "camera_look_at": {"x": 0.0, "y": 0.6, "z": 0.0},
        "camera_fov_deg": 50.0,
    },
    "thresholds": {"warn": 0.50, "danger": 0.20},
    "environment": {
        "temperature": {"warn_low": 5.0, "warn_high": 30.0},
        "humidity": {"warn_low": 25.0, "warn_high": 70.0},
    },
    "battery": {
        "warn_low_v": 12.4,
        "danger_low_v": 12.0,
        "charging_v": 13.4,
    },
}

_lock = threading.Lock()


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursive merge: override values into base, return new dict.

    Missing keys in override fall back to base. Lists/scalars in override fully replace base.
    """
    out = deepcopy(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = deepcopy(v)
    return out


def load() -> dict:
    with _lock:
        if not CONFIG_FILE.exists():
            return deepcopy(DEFAULTS)
        try:
            with CONFIG_FILE.open() as f:
                loaded = json.load(f)
            _strip_legacy_keys(loaded)
            return _deep_merge(DEFAULTS, loaded)
        except (json.JSONDecodeError, OSError):
            return deepcopy(DEFAULTS)


def save(updates: dict) -> dict:
    """Merge updates into current calibration and persist. Returns the new full calibration."""
    with _lock:
        current = load_unlocked()
        _strip_legacy_keys(updates)  # in case client still sends stale schema
        merged = _deep_merge(current, updates)
        _strip_legacy_keys(merged)
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_suffix(".json.tmp")
        with tmp.open("w") as f:
            json.dump(merged, f, indent=2)
        tmp.replace(CONFIG_FILE)
        return merged


def reset() -> dict:
    """Reset to defaults; deletes the config file."""
    with _lock:
        if CONFIG_FILE.exists():
            CONFIG_FILE.unlink()
        return deepcopy(DEFAULTS)


def load_unlocked() -> dict:
    """Internal: load without acquiring the lock (caller must hold it)."""
    if not CONFIG_FILE.exists():
        return deepcopy(DEFAULTS)
    try:
        with CONFIG_FILE.open() as f:
            loaded = json.load(f)
        _strip_legacy_keys(loaded)
        return _deep_merge(DEFAULTS, loaded)
    except (json.JSONDecodeError, OSError):
        return deepcopy(DEFAULTS)


def _strip_legacy_keys(d: dict) -> None:
    """Remove keys from older schemas so they can't shadow current ones.

    A top-level 'vehicle' (singular) was the old name for what's now
    vehicles.registry.<id>. Any save that includes 'vehicle' would have been
    cruft — read-side strip plus the save() path scrubs the file over time.
    """
    d.pop("vehicle", None)
