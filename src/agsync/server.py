"""FastAPI app factory.

Routes:
  /               -> redirect to /login (or /wizard if no admin yet)
  /login, /logout
  /wizard/*       -> 5-step setup
  /status         -> sync status + manual trigger
  /credentials    -> issued cards with install URL + QR code
  /logs           -> log viewer with filters
  /settings       -> connection edit + about
  /api/test-ag    -> wizard ajax connection test
  /api/test-pacs  -> wizard ajax connection test
  /connect/*      -> AG Connect hand-off for captcha-gated PACS logins
  /api/health     -> public, used by an external HC if anyone wires one
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from importlib.resources import files

from fastapi import FastAPI, Request, Response
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .auth import admin_exists, current_user
from .db import init_db
from .i18n import default_locale, get_translator
from .logs import install_handler as install_log_handler
from .routes import api, wizard
from .routes import auth as auth_routes
from .routes import connect as connect_route
from .routes import credentials as credentials_route
from .routes import logs as logs_route
from .routes import settings as settings_route
from .routes import status as status_route
from .settings_store import is_configured
from .sync import get_engine

logger = logging.getLogger(__name__)

LANG_COOKIE = "agsync_lang"


def _pacs_display_name() -> str:
    from .lib.pacs import get_descriptor
    from .settings_store import PacsConfig

    vendor = (PacsConfig.load() or {}).get("vendor", "")
    descriptor = get_descriptor(vendor) if vendor else None
    return descriptor.display_name if descriptor else "the PACS"


def _templates_dir() -> str:
    return str(files("agsync") / "templates")


def _static_dir() -> str:
    return str(files("agsync") / "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    install_log_handler()
    # First line of the run: the log survives restarts, so without it there
    # is no way to tell a quiet cycle from a process that went away.
    logger.info(
        "AccessGrid Sync v%s starting at %s",
        __version__, datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
    )
    engine = get_engine()
    if is_configured():
        engine.start()
    yield
    engine.stop()


def create_app() -> FastAPI:
    app = FastAPI(title="AccessGrid Sync", docs_url=None, redoc_url=None, lifespan=lifespan)

    templates = Jinja2Templates(directory=_templates_dir())

    def template_response(request: Request, name: str, ctx: dict | None = None):
        locale = request.cookies.get(LANG_COOKIE) or default_locale()
        translator = get_translator(locale)
        configured = is_configured()
        merged = {
            "request": request,
            "t": translator.t,
            "locale": locale,
            "available_locales": ["en", "es"],
            "configured": configured,
            "admin_exists": admin_exists(),
        }
        # Behind the session: the banner reveals that syncing is down and
        # carries a launch id, neither of which belongs on the login page.
        if configured and current_user(request) is not None:
            # Every page, not just the status screen: a PACS that has stopped
            # answering means nothing is syncing, and that should not be
            # discoverable only by visiting one particular page.
            merged["engine_status"] = get_engine().get_status()
            merged["pacs_display_name"] = _pacs_display_name()
        if ctx:
            merged.update(ctx)
        return templates.TemplateResponse(name, merged)

    app.state.template_response = template_response
    app.state.templates = templates

    app.mount("/static", StaticFiles(directory=_static_dir()), name="static")

    @app.exception_handler(StarletteHTTPException)
    async def auth_redirect(request: Request, exc: StarletteHTTPException):
        # require_admin raises 401 with a Location header when there's no
        # valid session. A 401 isn't a redirect, so turn it into one so an
        # unauthenticated visit to any protected page lands on /login.
        location = (exc.headers or {}).get("Location")
        if exc.status_code == 401 and location:
            if request.headers.get("HX-Request") == "true":
                # Let HTMX redirect the whole page instead of swapping the
                # login form into a polled fragment.
                return Response(status_code=200, headers={"HX-Redirect": location})
            return RedirectResponse(url=location, status_code=303)
        return await http_exception_handler(request, exc)

    @app.get("/")
    def index(request: Request):
        if not admin_exists() or not is_configured():
            return RedirectResponse(url="/wizard", status_code=303)
        if current_user(request) is None:
            return RedirectResponse(url="/login", status_code=303)
        return RedirectResponse(url="/status", status_code=303)

    app.include_router(auth_routes.router)
    app.include_router(wizard.router)
    app.include_router(status_route.router)
    app.include_router(credentials_route.router)
    app.include_router(logs_route.router)
    app.include_router(settings_route.router)
    app.include_router(api.router)
    app.include_router(connect_route.router)

    return app
