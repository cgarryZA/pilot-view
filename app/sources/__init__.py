import os

from app.sources.base import CameraSource
from app.sources.disconnected import DisconnectedSource
from app.sources.synthetic import SyntheticSource


_REGISTRY: dict[str, type[CameraSource]] = {
    "disconnected": DisconnectedSource,
    "synthetic": SyntheticSource,
}


def make_source() -> CameraSource:
    name = os.getenv("PILOT_VIEW_SOURCE", "disconnected").strip().lower()
    cls = _REGISTRY.get(name, DisconnectedSource)
    return cls()


# Singleton — created once at module import. main.py and automations.py both
# read from this so guards and the WS payload share the same instance.
source: CameraSource = make_source()


__all__ = [
    "CameraSource",
    "DisconnectedSource",
    "SyntheticSource",
    "make_source",
    "source",
]
