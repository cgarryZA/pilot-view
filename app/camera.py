from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CameraStatus:
    connected: bool = False
    model: str = "Orbbec Gemini 2"
    interface: str = "USB 3.0"
    last_attempt: Optional[str] = None
    error: Optional[str] = None
    serial: Optional[str] = None
    firmware: Optional[str] = None


class Camera:
    def __init__(self) -> None:
        self._status = CameraStatus()

    def status_dict(self) -> dict:
        s = self._status
        return {
            "connected": s.connected,
            "model": s.model,
            "interface": s.interface,
            "last_attempt": s.last_attempt,
            "error": s.error,
            "serial": s.serial,
            "firmware": s.firmware,
        }


camera = Camera()
