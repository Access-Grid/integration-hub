from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from ..auth import SESSION_COOKIE, sign_session, verify_admin_password
from ..auth.store import admin_exists

router = APIRouter()


@router.get("/login")
def login_page(request: Request):
    if not admin_exists():
        return RedirectResponse(url="/wizard", status_code=303)
    return request.app.state.template_response(request, "login.html", {"error": None})


@router.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    if not verify_admin_password(username, password):
        return request.app.state.template_response(
            request, "login.html", {"error": "login.invalid"}
        )
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
