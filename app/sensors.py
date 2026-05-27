"""Environment sensor surface (temperature + humidity).

Single Sensors singleton with a swappable driver. Default driver returns slowly
drifting synthetic values that stay roughly within "garage normal" range so the
warning pills don't constantly flicker. Switch to the SHT31 driver by setting
PILOT_VIEW_SENSORS=sht31 in ~/pilot-view/.env once the breakout is wired to the
Pi's I2C bus.

Adding more sensors (e.g. a second SHT31, an air-quality sensor) means adding
fields to the read() return — the UI/calibration auto-derives whichever
threshold-checked fields are present, so no scattered plumbing.
"""

import math
import os
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EnvironmentDriver(ABC):
    name: str = "base"

    @abstractmethod
    def read(self) -> dict:
        """Return current environment payload.

        Shape:
        {
          "available": bool,
          "source": str,
          "last_read": iso8601 | None,
          "temperature_c": float | None,
          "humidity_pct": float | None,
        }
        """


class SyntheticDriver(EnvironmentDriver):
    name = "synthetic"

    def __init__(self) -> None:
        self._t0 = time.monotonic()

    def read(self) -> dict:
        t = time.monotonic() - self._t0
        # Slow drift, ~1.5 minute period, gentle amplitude.
        temp = 21.0 + 3.5 * math.sin(t * 2 * math.pi / 90)
        humid = 52.0 + 14.0 * math.sin(t * 2 * math.pi / 130)
        return {
            "available": True,
            "source": self.name,
            "last_read": _now(),
            "temperature_c": round(temp, 1),
            "humidity_pct": round(humid, 1),
        }


class UnavailableDriver(EnvironmentDriver):
    name = "unavailable"

    def read(self) -> dict:
        return {
            "available": False,
            "source": self.name,
            "last_read": None,
            "temperature_c": None,
            "humidity_pct": None,
        }


class SHT31Driver(EnvironmentDriver):
    """Reads from an SHT31 over I2C. Currently a stub that fails import unless
    the user has wired the sensor + installed `adafruit-circuitpython-sht31d`.

    Importing this driver lazily so the rest of the app still starts on dev
    machines without the I2C stack.
    """
    name = "sht31"

    def __init__(self, address: int = 0x44) -> None:
        # Lazy imports — surface a clear error to caller if hardware libs missing.
        import board  # type: ignore
        import busio  # type: ignore
        import adafruit_sht31d  # type: ignore

        i2c = busio.I2C(board.SCL, board.SDA)
        self._dev = adafruit_sht31d.SHT31D(i2c, address=address)

    def read(self) -> dict:
        try:
            t = self._dev.temperature
            h = self._dev.relative_humidity
            return {
                "available": True,
                "source": self.name,
                "last_read": _now(),
                "temperature_c": round(float(t), 1),
                "humidity_pct": round(float(h), 1),
            }
        except Exception as exc:  # pragma: no cover — hardware-only path
            print(f"[sensors] SHT31 read failed: {exc}")
            return {
                "available": False,
                "source": self.name,
                "last_read": None,
                "temperature_c": None,
                "humidity_pct": None,
            }


def _make_driver() -> EnvironmentDriver:
    name = os.getenv("PILOT_VIEW_SENSORS", "synthetic").strip().lower()
    if name == "synthetic":
        return SyntheticDriver()
    if name == "sht31":
        try:
            return SHT31Driver()
        except Exception as exc:
            print(f"[sensors] SHT31 init failed, falling back to unavailable: {exc}")
            return UnavailableDriver()
    return UnavailableDriver()


class Sensors:
    def __init__(self) -> None:
        self._driver = _make_driver()

    def read(self) -> dict:
        return self._driver.read()


sensors = Sensors()
