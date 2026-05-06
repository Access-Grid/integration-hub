"""Signed session cookies.

The cookie value is a Fernet-signed token (using the same encryption
key as the rest of the app) carrying a JSON {sub, iat}. We treat it as
a sliding window: every authenticated request that's older than half
the lifetime gets a fresh cookie.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request, status
from itsdangerous import BadSignature, TimestampSigner

from ..config import get_settings

if TYPE_CHECKING:
    from collections.abc import Callable  # noqa: F401

SESSION_COOKIE = "agsync_session"
SESSION_TTL_S = 60 * 60  # 1 hour, sliding


def _signer() -> TimestampSigner:
    return TimestampSigner(get_settings().encryption_key)


def sign_session(username: str) -> str:
    payload = json.dumps({"sub": username, "iat": int(time.time())})
    return _signer().sign(payload.encode()).decode()


def verify_session(token: str) -> dict | None:
    try:
        raw = _signer().unsign(token, max_age=SESSION_TTL_S)
    except BadSignature:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def current_user(request: Request) -> dict | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return verify_session(token)


def require_admin(request: Request) -> dict:
    user = current_user(request)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"Location": "/login"},
        )
    return user
