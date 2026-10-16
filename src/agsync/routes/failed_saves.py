"""The refused-saves tab.

A read-only window onto exchanges the PACS rejected: what was sent, what
came back, and whatever reason it gave. The log carries the sentence; this
carries the evidence behind it.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from .. import failed_saves
from ..auth import require_admin

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/failed-saves")
def failed_saves_page(request: Request, _user=Depends(require_admin)):
    rows = failed_saves.recent()
    return request.app.state.template_response(
        request, "failed_saves.html", {"rows": rows},
    )


@router.post("/failed-saves/clear")
def clear_failed_saves(request: Request, _user=Depends(require_admin)):
    gone = failed_saves.clear()
    logger.info("Cleared %d refused save(s)", gone)
    return RedirectResponse(url="/failed-saves", status_code=303)
