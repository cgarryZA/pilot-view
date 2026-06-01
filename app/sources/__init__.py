import os
import threading
from pathlib import Path

from app.sources.base import CameraSource
from app.sources.disconnected import DisconnectedSource

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
ENV_FILE = PROJECT_ROOT / ".env"

# Deployment sources only: the real camera, plus a "disconnected" fallback for
# when it's unplugged. (The synthetic demo source was dev-only and removed.)
SOURCE_NAMES = ("disconnected", "orbbec")


def _orbbec_class():
    """Lazy import OrbbecSource so a machine without pyorbbecsdk still starts."""
    try:
        from app.sources.orbbec import OrbbecSource
        return OrbbecSource
    except Exception as exc:
        print(f"[sources] OrbbecSource unavailable: {exc}")
        return None


_REGISTRY: dict[str, type[CameraSource]] = {
    "disconnected": DisconnectedSource,
}


def _build(name: str) -> CameraSource:
    name = (name or "").strip().lower()
    # Reject unknown names (e.g. a stale PILOT_VIEW_SOURCE=synthetic left in .env
    # from before the synthetic source was removed). Fall back to the REAL camera,
    # not 'disconnected' — a silently-dead camera with a healthy-looking app is the
    # worst failure mode for a parking assistant.
    if name not in SOURCE_NAMES:
        print(f"[sources] unknown source '{name}'; falling back to 'orbbec'", flush=True)
        name = "orbbec"
    if name == "orbbec":
        cls = _orbbec_class()
        if cls is None:
            print("[sources] Falling back to DisconnectedSource (orbbec import failed)", flush=True)
            cls = DisconnectedSource
        return cls()
    cls = _REGISTRY.get(name, DisconnectedSource)
    return cls()


def make_source() -> CameraSource:
    requested = os.getenv("PILOT_VIEW_SOURCE", "orbbec")
    src = _build(requested)
    print(f"[sources] startup source = '{src.name}' (requested '{requested}')", flush=True)
    return src


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
    """Holds the active CameraSource and allows runtime swapping. Other modules
    import the manager (not a particular source) and call .state() on it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: CameraSource = make_source()

    @property
    def name(self) -> str:
        return self._current.name

    def state(self) -> dict:
        with self._lock:
            src = self._current
        return src.state()

    def get_jpeg(self):
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
    "SourceManager",
    "make_source",
    "source_manager",
    "SOURCE_NAMES",
]
