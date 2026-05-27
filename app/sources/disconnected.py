from app.sources.base import CameraSource


class DisconnectedSource(CameraSource):
    name = "disconnected"

    def state(self) -> dict:
        return {
            "source": self.name,
            "live_url": None,
            "camera": {
                "connected": False,
                "model": "Orbbec Gemini 2",
                "interface": "USB 3.0",
                "serial": None,
                "firmware": None,
                "last_attempt": None,
                "error": None,
            },
            "geometry": None,
        }
