import os
import threading
from pathlib import Path

from app.sources.base import CameraSource
from app.sources.disconnected import DisconnectedSource
from app.sources.synthetic import SyntheticSource

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
ENV_FILE = PROJECT_ROOT / ".env"

SOURCE_NAMES = ("disconnected", "synthetic", "orbbec")


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


def _build(name: str) -> CameraSource:
    name = (name or "").strip().lower()
    if name == "orbbec":
        cls = _orbbec_class()
        if cls is None:
            print("[sources] Falling back to DisconnectedSource (orbbec import failed)")
            cls = DisconnectedSource
        return cls()
    cls = _REGISTRY.get(name, DisconnectedSource)
    return cls()


def make_source() -> CameraSource:
    return _build(os.getenv("PILOT_VIEW_SOURCE", "disconnected"))


def _persist_to_env(name: str) -> None:
    """Write PILOT_VIEW_SOURCE=<name> into ~/pilot-view/.env, preserving other keys."""
    lines: list[str] = []
    if ENV_FILE.exists():
        try:
            lines = ENV_FILE.read_text().splitlines()
        except OSError:
            lines = []
    found = False
    out: list[str] = []
    for line in lines:
        if line.startswith("PILOT_VIEW_SOURCE="):
            out.append(f"PILOT_VIEW_SOURCE={name}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"PILOT_VIEW_SOURCE={name}")
    try:
        ENV_FILE.write_text("\n".join(out) + "\n")
    except OSError as exc:
        print(f"[sources] could not persist env: {exc}")


class SourceManager:
    """Holds the active CameraSource and allows runtime swapping.

    Other modules import the manager (not a particular source instance) and call
    .state() on it. That way switching cameras doesn't require re-importing or
    restarting the service.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: CameraSource = make_source()

    @property
    def name(self) -> str:
        return self._current.name

    def state(self) -> dict:
        # Source.state() is itself thread-safe enough for our purposes; pulling
        # the reference under the lock guards against a concurrent swap.
        with self._lock:
            src = self._current
        return src.state()

    def get_jpeg(self):
        """Return the latest JPEG frame from the current source, or None.

        Only sources with a video stream (orbbec) implement get_jpeg.
        """
        with self._lock:
            src = self._current
        fn = getattr(src, "get_jpeg", None)
        return fn() if callable(fn) else None

    def get_depth(self):
        """Return (buf, scale, intr) from the current source's depth stream, or
        None. Only sources that capture depth (orbbec) implement get_depth."""
        with self._lock:
            src = self._current
        fn = getattr(src, "get_depth", None)
        return fn() if callable(fn) else None

    def switch(self, name: str, persist: bool = True) -> str:
        name = (name or "").strip().lower()
        if name not in SOURCE_NAMES:
            raise ValueError(f"unknown source '{name}'")
        with self._lock:
            old = self._current
            # Best-effort cleanup if the source exposes a close hook (orbbec might,
            # eventually). Silent on failure — we still want the swap to proceed.
            close = getattr(old, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:
                    print(f"[sources] cleanup of {old.name} failed: {exc}")
            self._current = _build(name)
        if persist:
            _persist_to_env(name)
        return self._current.name


source_manager = SourceManager()


__all__ = [
    "CameraSource",
    "DisconnectedSource",
    "SyntheticSource",
    "SourceManager",
    "make_source",
    "source_manager",
    "SOURCE_NAMES",
]
