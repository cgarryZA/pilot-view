from abc import ABC, abstractmethod


class CameraSource(ABC):
    name: str = "base"

    @abstractmethod
    def state(self) -> dict:
        """Return the full state payload sent over the WebSocket each tick.

        Shape:
        {
          "source": str,
          "camera": {
            "connected": bool,
            "model": str,
            "interface": str,
            "serial": str | None,
            "firmware": str | None,
            "last_attempt": iso8601 | None,
            "error": str | None,
          },
          "geometry": {                         # only when connected
            "garage": {...},
            "car": {...},
            "clearances": {...},
            "state": "safe" | "warning" | "danger",
          } | None,
        }
        """
