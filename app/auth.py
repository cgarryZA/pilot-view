"""WebAuthn (passkey) registration & authentication.

Endpoints under /api/auth/*. Each flow is begin → client biometric → complete.
First-time registration is unauthenticated *if* no passkeys are registered yet
(trust-on-first-use). Subsequent registrations require an existing session.

Dev escape hatch: set PILOT_VIEW_AUTH=disabled to bypass all auth checks. Useful
when iterating on UI before Tailscale/HTTPS is set up.
"""

import base64
import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.cose import COSEAlgorithmIdentifier
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from app.passkeys import (
    SESSION_COOKIE_NAME,
    SESSION_TTL,
    passkeys,
    sessions,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

RP_NAME = "Pilot View"
CHALLENGE_TTL_SECONDS = 300

# Map of challenge_id → {challenge: bytes, kind: 'register'|'login', expires: monotonic_seconds, ...}
_pending: dict[str, dict] = {}
_pending_lock = threading.Lock()


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _expected_rp_id(request: Request) -> str:
    rp_id = os.getenv("PILOT_VIEW_RP_ID", "").strip()
    if rp_id:
        return rp_id
    return request.url.hostname or "localhost"


def _expected_origin(request: Request) -> str:
    scheme = request.url.scheme
    netloc = request.url.netloc
    return f"{scheme}://{netloc}"


def _gc_pending() -> None:
    """Remove expired challenges; cheap to call before each issue."""
    now = time.monotonic()
    with _pending_lock:
        for cid in [k for k, v in _pending.items() if v["expires"] < now]:
            del _pending[cid]


def _stash_challenge(challenge: bytes, kind: str, extra: Optional[dict] = None) -> str:
    _gc_pending()
    challenge_id = secrets.token_urlsafe(16)
    with _pending_lock:
        _pending[challenge_id] = {
            "challenge": challenge,
            "kind": kind,
            "expires": time.monotonic() + CHALLENGE_TTL_SECONDS,
            **(extra or {}),
        }
    return challenge_id


def _take_challenge(challenge_id: str, kind: str) -> dict:
    with _pending_lock:
        record = _pending.pop(challenge_id, None)
    if not record:
        raise HTTPException(400, "challenge expired or not found")
    if record["expires"] < time.monotonic():
        raise HTTPException(400, "challenge expired")
    if record["kind"] != kind:
        raise HTTPException(400, "challenge kind mismatch")
    return record


def _auth_disabled() -> bool:
    return os.getenv("PILOT_VIEW_AUTH", "enabled").strip().lower() == "disabled"


# ─── FastAPI dependency for protected endpoints ────────────────────────

def current_session(request: Request) -> Optional[dict]:
    sid = request.cookies.get(SESSION_COOKIE_NAME)
    if not sid:
        return None
    return sessions.get(sid)


def require_auth(request: Request) -> dict:
    if _auth_disabled():
        return {"credential_id": "_dev_", "nickname": "dev mode"}
    s = current_session(request)
    if not s:
        raise HTTPException(401, "auth required")
    return s


# ─── Cookie handling ───────────────────────────────────────────────────

def _set_session_cookie(response: Response, session_id: str, secure: bool) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_id,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        samesite="strict",
        secure=secure,
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")


# ─── Registration ──────────────────────────────────────────────────────

@router.post("/register/begin")
async def register_begin(request: Request, payload: dict):
    """Start passkey registration.

    First registration is unauthenticated (trust on first use). Subsequent
    registrations require an active session.
    """
    if not _auth_disabled() and passkeys.count() > 0:
        if not current_session(request):
            raise HTTPException(401, "must be signed in to register another device")

    nickname = (payload or {}).get("nickname", "").strip() or "Unnamed device"

    rp_id = _expected_rp_id(request)
    user_id = secrets.token_bytes(16)
    options = generate_registration_options(
        rp_id=rp_id,
        rp_name=RP_NAME,
        user_id=user_id,
        user_name="user",
        user_display_name="Pilot View user",
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=_b64url_decode(c))
            for c in passkeys.all_credential_ids()
        ],
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        supported_pub_key_algs=[
            COSEAlgorithmIdentifier.ECDSA_SHA_256,
            COSEAlgorithmIdentifier.RSASSA_PKCS1_v1_5_SHA_256,
        ],
    )
    challenge_id = _stash_challenge(options.challenge, "register", {"nickname": nickname})
    return {
        "challenge_id": challenge_id,
        "options": json.loads(options_to_json(options)),
    }


@router.post("/register/complete")
async def register_complete(request: Request, payload: dict):
    challenge_id = payload.get("challenge_id")
    credential_raw = payload.get("credential")
    if not challenge_id or not credential_raw:
        raise HTTPException(400, "challenge_id and credential required")

    record = _take_challenge(challenge_id, "register")

    try:
        verification = verify_registration_response(
            credential=credential_raw,
            expected_challenge=record["challenge"],
            expected_rp_id=_expected_rp_id(request),
            expected_origin=_expected_origin(request),
            require_user_verification=True,
        )
    except Exception as exc:
        raise HTTPException(400, f"registration verification failed: {exc}")

    credential_id = _b64url_encode(verification.credential_id)
    public_key = _b64url_encode(verification.credential_public_key)
    sign_count = verification.sign_count
    nickname = record.get("nickname") or "Unnamed device"

    entry = passkeys.add(credential_id, public_key, sign_count, nickname)

    # Auto-sign-in after registration so the user doesn't have to immediately authenticate.
    session_id = sessions.create(credential_id, nickname)
    response = Response(content='{"ok":true}', media_type="application/json")
    _set_session_cookie(response, session_id, secure=request.url.scheme == "https")
    response.headers["X-Auth-Action"] = "registered"
    return response


# ─── Login ─────────────────────────────────────────────────────────────

@router.post("/login/begin")
async def login_begin(request: Request):
    if passkeys.count() == 0:
        raise HTTPException(400, "no passkeys registered yet")

    options = generate_authentication_options(
        rp_id=_expected_rp_id(request),
        allow_credentials=[
            PublicKeyCredentialDescriptor(id=_b64url_decode(c))
            for c in passkeys.all_credential_ids()
        ],
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    challenge_id = _stash_challenge(options.challenge, "login")
    return {
        "challenge_id": challenge_id,
        "options": json.loads(options_to_json(options)),
    }


@router.post("/login/complete")
async def login_complete(request: Request, payload: dict):
    challenge_id = payload.get("challenge_id")
    credential_raw = payload.get("credential")
    if not challenge_id or not credential_raw:
        raise HTTPException(400, "challenge_id and credential required")

    record = _take_challenge(challenge_id, "login")

    # Look up the credential by id (URL-safe base64 of the raw id)
    raw_credential_id = credential_raw.get("rawId") or credential_raw.get("id")
    if not raw_credential_id:
        raise HTTPException(400, "credential id missing")
    stored = passkeys.find(raw_credential_id)
    if not stored:
        raise HTTPException(400, "unknown credential")

    try:
        verification = verify_authentication_response(
            credential=credential_raw,
            expected_challenge=record["challenge"],
            expected_rp_id=_expected_rp_id(request),
            expected_origin=_expected_origin(request),
            credential_public_key=_b64url_decode(stored["public_key"]),
            credential_current_sign_count=stored["sign_count"],
            require_user_verification=True,
        )
    except Exception as exc:
        raise HTTPException(400, f"authentication verification failed: {exc}")

    passkeys.mark_used(stored["credential_id"], verification.new_sign_count)
    session_id = sessions.create(stored["credential_id"], stored["nickname"])

    response = Response(content='{"ok":true}', media_type="application/json")
    _set_session_cookie(response, session_id, secure=request.url.scheme == "https")
    return response


# ─── Logout / status / device management ───────────────────────────────

@router.post("/logout")
async def logout(request: Request):
    sid = request.cookies.get(SESSION_COOKIE_NAME)
    if sid:
        sessions.destroy(sid)
    response = Response(content='{"ok":true}', media_type="application/json")
    _clear_session_cookie(response)
    return response


@router.get("/status")
async def status(request: Request):
    if _auth_disabled():
        return {
            "authenticated": True,
            "auth_disabled": True,
            "has_passkeys": passkeys.count() > 0,
            "session": {"nickname": "dev mode"},
        }
    s = current_session(request)
    return {
        "authenticated": s is not None,
        "auth_disabled": False,
        "has_passkeys": passkeys.count() > 0,
        "session": (
            {"nickname": s["nickname"]} if s else None
        ),
    }


@router.get("/passkeys")
async def list_passkeys(request: Request):
    if not _auth_disabled():
        require_auth(request)
    return {"passkeys": passkeys.list_public()}


@router.delete("/passkeys/{credential_id}")
async def revoke_passkey(credential_id: str, request: Request):
    if not _auth_disabled():
        require_auth(request)
    ok = passkeys.revoke(credential_id)
    if not ok:
        raise HTTPException(404, "credential not found")
    sessions.destroy_for_credential(credential_id)
    return {"ok": True}


@router.post("/passkeys/{credential_id}/rename")
async def rename_passkey(credential_id: str, payload: dict, request: Request):
    if not _auth_disabled():
        require_auth(request)
    nickname = (payload or {}).get("nickname", "").strip()
    if not nickname:
        raise HTTPException(400, "nickname required")
    ok = passkeys.rename(credential_id, nickname)
    if not ok:
        raise HTTPException(404, "credential not found")
    return {"ok": True}
