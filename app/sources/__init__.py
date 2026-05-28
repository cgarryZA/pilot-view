import os

from app.sources.base import CameraSource
from app.sources.disconnected import DisconnectedSource
from app.sources.synthetic import SyntheticSource


def _orbbec_class():
    """Lazy import OrbbecSource so dev machines without pyorbbecsdk still start."""
    try:
        from app.sources.orbbec import OrbbecSource
        return OrbbecSource
    except Exception as exc:
        print(f"[sources] OrbbecSource unavailable: {exc}")
        return None


_REGISTRY: dict[str, type[CameraSource]] = {
    "disconnected": DisconnectedSource,
    "synthetic": SyntheticSource,
}


def make_source() -> CameraSource:
    name = os.getenv("PILOT_VIEW_SOURCE", "disconnected").strip().lower()
    if name == "orbbec":
        cls = _orbbec_class()
        if cls is None:
            print("[sources] Falling back to DisconnectedSource (orbbec import failed)")
            cls = DisconnectedSource
        return cls()
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
