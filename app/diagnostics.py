"""System diagnostics — CPU / memory / disk / Pi temperature / process info.

Reads everything from /proc and /sys to avoid pulling in psutil. Works on
Raspberry Pi OS and any modern Linux. On non-Linux dev hosts the temperature
reading silently returns None and the rest still works.
"""

import os
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _CPUTracker:
    """CPU% is a delta between snapshots of /proc/stat. Cache the previous read."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._prev = self._read()

    def _read(self) -> tuple[int, int]:
        try:
            with open("/proc/stat", "r") as f:
                line = f.readline()
        except FileNotFoundError:
            return (0, 0)
        # cpu user nice system idle iowait irq softirq steal guest guest_nice
        parts = line.split()
        values = list(map(int, parts[1:]))
        # Pad short outputs (older kernels) with zeros.
        while len(values) < 8:
            values.append(0)
        idle = values[3] + values[4]
        total = sum(values)
        return (idle, total)

    def percent(self) -> Optional[float]:
        with self._lock:
            idle, total = self._read()
            prev_idle, prev_total = self._prev
            self._prev = (idle, total)
        if total == 0:
            return None
        delta_total = total - prev_total
        delta_idle = idle - prev_idle
        if delta_total <= 0:
            return 0.0
        return round(100.0 * (1.0 - delta_idle / delta_total), 1)


def _read_meminfo() -> dict:
    try:
        info: dict[str, int] = {}
        with open("/proc/meminfo", "r") as f:
            for line in f:
                key, _, rest = line.partition(":")
                value = rest.strip().split()[0]
                info[key] = int(value)  # values are in kB
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        used = total - avail
        return {
            "total_mb": round(total / 1024, 1),
            "used_mb": round(used / 1024, 1),
            "percent": round(100.0 * used / total, 1) if total else 0.0,
        }
    except FileNotFoundError:
        return {"total_mb": None, "used_mb": None, "percent": None}


def _read_disk() -> dict:
    try:
        usage = shutil.disk_usage("/")
        gb = 1024 * 1024 * 1024
        return {
            "total_gb": round(usage.total / gb, 1),
            "used_gb": round(usage.used / gb, 1),
            "percent": round(100.0 * usage.used / usage.total, 1),
        }
    except OSError:
        return {"total_gb": None, "used_gb": None, "percent": None}


def _read_cpu_temp() -> Optional[float]:
    # /sys/class/thermal/thermal_zone0/temp on Pi/Linux, in millidegrees C.
    paths = [
        "/sys/class/thermal/thermal_zone0/temp",
        "/sys/class/hwmon/hwmon0/temp1_input",
    ]
    for p in paths:
        try:
            raw = Path(p).read_text().strip()
            return round(int(raw) / 1000.0, 1)
        except (FileNotFoundError, OSError, ValueError):
            continue
    return None


def _read_load_avg() -> Optional[tuple[float, float, float]]:
    try:
        a, b, c = os.getloadavg()
        return (round(a, 2), round(b, 2), round(c, 2))
    except (OSError, AttributeError):
        return None


def _read_process_memory() -> Optional[float]:
    try:
        with open("/proc/self/status", "r") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    kb = int(line.split()[1])
                    return round(kb / 1024, 1)  # MB
    except FileNotFoundError:
        pass
    return None


def _read_system_uptime() -> Optional[float]:
    try:
        with open("/proc/uptime", "r") as f:
            return float(f.read().split()[0])
    except FileNotFoundError:
        return None


_PROCESS_START = time.monotonic()
_cpu = _CPUTracker()


def snapshot() -> dict:
    cpu_pct = _cpu.percent()
    load = _read_load_avg()
    return {
        "ts": _now(),
        "cpu": {
            "percent": cpu_pct,
            "load_avg": list(load) if load else None,
            "cores": os.cpu_count(),
        },
        "memory": _read_meminfo(),
        "disk": _read_disk(),
        "temperature": {"cpu_c": _read_cpu_temp()},
        "process": {
            "uptime_s": round(time.monotonic() - _PROCESS_START, 1),
            "memory_mb": _read_process_memory(),
        },
        "system": {"uptime_s": _read_system_uptime()},
    }
