from __future__ import annotations

import threading
import time
from math import ceil

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from ..auth import SESSION_COOKIE, sign_session, verify_admin_password
from ..auth.store import admin_exists
from ..db.connection import execute_one, get_db
from ..i18n import get_translator

router = APIRouter()

_LOCK = threading.Lock()
_FAIL_WINDOW_S = 15 * 60
_MAX_FAILS = 8
_LOCKOUT_BASE_S = 60
_LOCKOUT_MAX_S = 24 * 60 * 60


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    host = request.client.host if request.client else ""
    return host or "unknown"


def _login_key(request: Request, username: str) -> str:
    return f"{_client_ip(request)}|{username.strip().lower()}"


def _is_locked(key: str) -> bool:
    now = time.time()
    with _LOCK:
        row = execute_one(
            "SELECT locked_until FROM login_attempts WHERE key = ?",
            (key,),
        )
        until = float(row["locked_until"]) if row and row["locked_until"] else 0.0
        if until <= now:
            get_db().execute(
                "UPDATE login_attempts SET locked_until = NULL WHERE key = ?",
                (key,),
            )
            return False
        return True


def _lockout_remaining_s(key: str) -> int:
    now = time.time()
    with _LOCK:
        row = execute_one(
            "SELECT locked_until FROM login_attempts WHERE key = ?",
            (key,),
        )
        until = float(row["locked_until"]) if row and row["locked_until"] else 0.0
    return max(0, int(ceil(until - now)))


def _record_failure(key: str) -> None:
    now = time.time()
    window_start = now - _FAIL_WINDOW_S
    with _LOCK:
        row = execute_one(
            "SELECT fail_count, first_fail_at, lock_level FROM login_attempts WHERE key = ?",
            (key,),
        )
        fail_count = int(row["fail_count"]) if row else 0
        first_fail_at = float(row["first_fail_at"]) if row and row["first_fail_at"] else 0.0
        lock_level = int(row["lock_level"]) if row else 0
        if first_fail_at < window_start:
            fail_count = 1
            first_fail_at = now
        else:
            fail_count += 1
        locked_until = None
        if fail_count >= _MAX_FAILS:
            lock_level += 1
            lockout_s = min(_LOCKOUT_BASE_S * (2 ** (lock_level - 1)), _LOCKOUT_MAX_S)
            locked_until = now + lockout_s
            fail_count = 0
            first_fail_at = now
        get_db().execute(
            """
            INSERT INTO login_attempts (key, fail_count, first_fail_at, locked_until, lock_level)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                fail_count = excluded.fail_count,
                first_fail_at = excluded.first_fail_at,
                locked_until = excluded.locked_until,
                lock_level = excluded.lock_level
            """,
            (key, fail_count, first_fail_at, locked_until, lock_level),
        )


def _record_success(key: str) -> None:
    with _LOCK:
        get_db().execute("DELETE FROM login_attempts WHERE key = ?", (key,))


@router.get("/login")
def login_page(request: Request):
    if not admin_exists():
        return RedirectResponse(url="/wizard", status_code=303)
    return request.app.state.template_response(request, "login.html", {"error": None})


@router.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    key = _login_key(request, username)
    if _is_locked(key):
        remaining_s = _lockout_remaining_s(key)
        remaining_m = max(1, int(ceil(remaining_s / 60)))
        locale = request.cookies.get("agsync_lang") or "en"
        rate_limit_key = "login.rate_limited.one" if remaining_m == 1 else "login.rate_limited.many"
        error_text = get_translator(locale).t(rate_limit_key, minutes=remaining_m)
        return request.app.state.template_response(
            request,
            "login.html",
            {"error": None, "error_text": error_text},
        )
    if not verify_admin_password(username, password):
        _record_failure(key)
        return request.app.state.template_response(
            request, "login.html", {"error": "login.invalid"}
        )
    _record_success(key)
    response = RedirectResponse(url="/status", status_code=303)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=sign_session(username),
        max_age=3600,
        httponly=True,
        samesite="lax",
    )
    return response


@router.post("/logout")
def logout(request: Request):
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@router.get("/lang/{locale}")
def set_lang(locale: str, request: Request):
    referer = request.headers.get("Referer", "/")
    response = RedirectResponse(url=referer, status_code=303)
    if locale in ("en", "es"):
        response.set_cookie("agsync_lang", locale, max_age=60 * 60 * 24 * 365, samesite="lax")
    return response
