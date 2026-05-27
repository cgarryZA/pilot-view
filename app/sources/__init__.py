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


__all__ = ["CameraSource", "DisconnectedSource", "SyntheticSource", "make_source"]
