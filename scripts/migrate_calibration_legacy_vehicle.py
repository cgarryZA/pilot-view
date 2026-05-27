#!/usr/bin/env python3
"""One-off migration.

Earlier versions of Pilot View stored vehicle calibration under a single
top-level "vehicle" key. We've since moved everything per-vehicle into
"vehicles.registry.<id>". Any installation that was edited under the old
schema will have a stale "vehicle" block at the top level that the frontend
was mistakenly writing into.

This script pulls those fields into the currently-active vehicle's registry
entry and removes the legacy key. Safe to re-run — does nothing if the
legacy key is already absent.
"""
import json
import sys
from pathlib import Path

PATH = Path(__file__).resolve().parent.parent / "config" / "calibration.json"


def main() -> int:
    if not PATH.exists():
        print(f"{PATH} does not exist; nothing to migrate.")
        return 0

    with PATH.open() as f:
        cal = json.load(f)

    legacy = cal.pop("vehicle", None)
    if not legacy:
        print("No legacy 'vehicle' block — already migrated or never affected.")
        return 0

    active = cal.get("vehicles", {}).get("active_id") or "lambo"
    registry = (
        cal.setdefault("vehicles", {})
        .setdefault("registry", {})
        .setdefault(active, {})
    )
    for k, v in legacy.items():
        registry[k] = v

    with PATH.open("w") as f:
        json.dump(cal, f, indent=2)

    print(f"Merged legacy 'vehicle' block into vehicles.registry.{active}")
    print("Keys migrated:", ", ".join(sorted(legacy.keys())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
