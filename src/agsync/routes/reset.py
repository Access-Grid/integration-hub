"""Factory reset.

  GET  /reset  -> confirmation page (warns what will be erased)
  POST /reset  -> stop the engine, wipe all data, redirect to /wizard

Intentionally unauthenticated so the install can be recovered even when
the admin password is lost. The GET confirmation page is the guard against
accidental wipes (prefetch, bookmarks, a stray click).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from ..db.connection import wipe_all_data
from ..sync import get_engine

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/reset")
def reset_page(request: Request):
    return request.app.state.template_response(request, "reset.html")


@router.post("/reset")
def reset_submit(request: Request):
    # Stop the background sync thread before pulling its config out from
    # under it; the wizard restarts it once setup is completed again.
    get_engine().stop()
    wipe_all_data()
    logger.warning("System reset via /reset — all data wiped, returning to wizard")
    return RedirectResponse(url="/wizard", status_code=303)
