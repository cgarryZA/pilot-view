"""Persistent passkey (WebAuthn credential) registry + in-memory session store.

Passkeys live at config/passkeys.json — one entry per registered device.
Sessions are in-memory only (lost on restart); cookie carries the session id.
A 30-day cookie + sliding refresh means each device logs in once a month at most.

NOT in this file: the WebAuthn challenge/verification logic — that's in app/auth.py.
This is just the data layer.
"""

import json
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
PASSKEYS_FILE = CONFIG_DIR / "passkeys.json"
SESSIONS_FILE = CONFIG_DIR / "sessions.json"

SESSION_TTL = timedelta(days=30)
SESSION_COOKIE_NAME = "pv_session"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─── Passkey registry ──────────────────────────────────────────────────

class PasskeyStore:
    """Thread-safe load/save of registered WebAuthn credentials.

    Schema (per credential):
      {
        "credential_id": str (base64url),
        "public_key":   str (base64url),
        "sign_count":   int,
        "nickname":     str,
        "registered_at": iso8601,
        "last_used_at":  iso8601 | None,
      }
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._creds: list[dict] = self._load_unlocked()

    def _load_unlocked(self) -> list[dict]:
        if not PASSKEYS_FILE.exists():
            return []
        try:
            with PASSKEYS_FILE.open() as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    def _save_unlocked(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = PASSKEYS_FILE.with_suffix(".json.tmp")
        with tmp.open("w") as f:
            json.dump(self._creds, f, indent=2)
        tmp.replace(PASSKEYS_FILE)

    def count(self) -> int:
        with self._lock:
            return len(self._creds)

    def list_public(self) -> list[dict]:
        """Public projection — no key material, safe to send to client."""
        with self._lock:
            return [
                {
                    "credential_id": c["credential_id"],
                    "nickname": c.get("nickname", "Unnamed device"),
                    "registered_at": c.get("registered_at"),
                    "last_used_at": c.get("last_used_at"),
                }
                for c in self._creds
            ]

    def all_credential_ids(self) -> list[str]:
        with self._lock:
            return [c["credential_id"] for c in self._creds]

    def add(
        self,
        credential_id: str,
        public_key: str,
        sign_count: int,
        nickname: str,
    ) -> dict:
        with self._lock:
            # Replace if same credential_id (shouldn't happen normally, but be safe)
            self._creds = [c for c in self._creds if c["credential_id"] != credential_id]
            entry = {
                "credential_id": credential_id,
                "public_key": public_key,
                "sign_count": sign_count,
                "nickname": nickname,
                "registered_at": _now().isoformat(),
                "last_used_at": None,
            }
            self._creds.append(entry)
            self._save_unlocked()
            return entry

    def find(self, credential_id: str) -> Optional[dict]:
        with self._lock:
            for c in self._creds:
                if c["credential_id"] == credential_id:
                    return dict(c)
        return None

    def mark_used(self, credential_id: str, new_sign_count: int) -> None:
        with self._lock:
            for c in self._creds:
                if c["credential_id"] == credential_id:
                    c["sign_count"] = new_sign_count
                    c["last_used_at"] = _now().isoformat()
                    self._save_unlocked()
                    return

    def revoke(self, credential_id: str) -> bool:
        with self._lock:
            before = len(self._creds)
            self._creds = [c for c in self._creds if c["credential_id"] != credential_id]
            if len(self._creds) != before:
                self._save_unlocked()
                return True
            return False

    def rename(self, credential_id: str, nickname: str) -> bool:
        with self._lock:
            for c in self._creds:
                if c["credential_id"] == credential_id:
                    c["nickname"] = nickname
                    self._save_unlocked()
                    return True
            return False


passkeys = PasskeyStore()


# ─── Session store (in-memory) ─────────────────────────────────────────

class SessionStore:
    """In-memory sessions, persisted to disk so a service restart (e.g. a deploy)
    doesn't log everyone out. Only created_at drives the 30-day TTL; last_used_at
    is best-effort and not flushed on every request (too chatty)."""

    def __init__(self) -> None:
        self._sessions: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if not SESSIONS_FILE.exists():
            return
        try:
            with SESSIONS_FILE.open() as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return
        now = _now()
        for sid, d in (data or {}).items():
            try:
                created = datetime.fromisoformat(d["created_at"])
            except (KeyError, ValueError):
                continue
            if now - created > SESSION_TTL:
                continue
            self._sessions[sid] = {
                "credential_id": d.get("credential_id", ""),
                "nickname": d.get("nickname", "device"),
                "created_at": created,
                "last_used_at": created,
            }

    def _save_unlocked(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            sid: {
                "credential_id": d["credential_id"],
                "nickname": d["nickname"],
                "created_at": d["created_at"].isoformat(),
                "last_used_at": d["last_used_at"].isoformat(),
            }
            for sid, d in self._sessions.items()
        }
        tmp = SESSIONS_FILE.with_suffix(".json.tmp")
        try:
            with tmp.open("w") as f:
                json.dump(data, f)
            tmp.replace(SESSIONS_FILE)
        except OSError as exc:
            print(f"[sessions] save failed: {exc}")

    def create(self, credential_id: str, nickname: str) -> str:
        session_id = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[session_id] = {
                "credential_id": credential_id,
                "nickname": nickname,
                "created_at": _now(),
                "last_used_at": _now(),
            }
            self._save_unlocked()
        return session_id

    def get(self, session_id: str) -> Optional[dict]:
        with self._lock:
            data = self._sessions.get(session_id)
            if not data:
                return None
            if _now() - data["created_at"] > SESSION_TTL:
                del self._sessions[session_id]
                self._save_unlocked()
                return None
            data["last_used_at"] = _now()  # in-memory only; not flushed per-request
            return dict(data)

    def destroy(self, session_id: str) -> None:
        with self._lock:
            if self._sessions.pop(session_id, None) is not None:
                self._save_unlocked()

    def destroy_for_credential(self, credential_id: str) -> int:
        with self._lock:
            to_kill = [sid for sid, d in self._sessions.items() if d["credential_id"] == credential_id]
            for sid in to_kill:
                del self._sessions[sid]
            if to_kill:
                self._save_unlocked()
            return len(to_kill)


sessions = SessionStore()


def session_from_cookies(cookies) -> Optional[dict]:
    """The single source of truth for 'is this request logged in?'. Returns the
    valid session dict for a cookie jar (Starlette request/websocket .cookies),
    or None. Shared by require_auth, /ws, and the terminal so they can't drift."""
    try:
        sid = cookies.get(SESSION_COOKIE_NAME)
    except AttributeError:
        sid = None
    return sessions.get(sid) if sid else None
