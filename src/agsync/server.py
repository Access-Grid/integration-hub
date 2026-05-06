"""FastAPI app factory.

Routes:
  /               -> redirect to /login (or /wizard if no admin yet)
  /login, /logout
  /wizard/*       -> 5-step setup
  /status         -> sync status + manual trigger
  /logs           -> log viewer with filters
  /settings       -> connection edit + about
  /api/test-ag    -> wizard ajax connection test
  /api/test-pacs  -> wizard ajax connection test
  /api/health     -> public, used by an external HC if anyone wires one
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from importlib.resources import files

from .auth import admin_exists
from .db import init_db
from .i18n import default_locale, get_translator
from .logs import install_handler as install_log_handler
from .routes import api, auth as auth_routes, logs as logs_route, settings as settings_route, status as status_route, wizard
from .sync import get_engine
from .settings_store import is_configured

logger = logging.getLogger(__name__)

LANG_COOKIE = "agsync_lang"


def _templates_dir() -> str:
    return str(files("agsync") / "templates")


def _static_dir() -> str:
    return str(files("agsync") / "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    install_log_handler()
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
        merged = {
            "request": request,
            "t": translator.t,
            "locale": locale,
            "available_locales": ["en", "es"],
            "configured": is_configured(),
            "admin_exists": admin_exists(),
        }
        if ctx:
            merged.update(ctx)
        return templates.TemplateResponse(name, merged)

    app.state.template_response = template_response
    app.state.templates = templates

    app.mount("/static", StaticFiles(directory=_static_dir()), name="static")

    @app.get("/")
    def index(request: Request):
        if not admin_exists() or not is_configured():
            return RedirectResponse(url="/wizard", status_code=303)
        return RedirectResponse(url="/status", status_code=303)

    app.include_router(auth_routes.router)
    app.include_router(wizard.router)
    app.include_router(status_route.router)
    app.include_router(logs_route.router)
    app.include_router(settings_route.router)
    app.include_router(api.router)

    return app
