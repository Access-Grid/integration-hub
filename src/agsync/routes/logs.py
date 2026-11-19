from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse

from ..auth import require_admin
from ..logs import query_logs, recent_text

router = APIRouter()

_VALID_LEVELS = {"ALL", "DEBUG", "INFO", "WARNING", "ERROR"}


@router.get("/logs")
def logs_page(
    request: Request,
    level: str = Query("ALL"),
    phase: str = Query(""),
    q: str = Query(""),
    _user=Depends(require_admin),
):
    level = level.upper() if level.upper() in _VALID_LEVELS else "ALL"
    rows = query_logs(level=level, phase=phase or None, search=q or None, limit=500)
    return request.app.state.template_response(
        request, "logs.html",
        {"rows": rows, "level": level, "phase": phase, "q": q},
    )


@router.get("/logs/download")
def logs_download(_user=Depends(require_admin)):
    """The newest 5,000 entries as logs.txt, to send to whoever is debugging."""
    return PlainTextResponse(
        recent_text(),
        headers={"Content-Disposition": 'attachment; filename="logs.txt"', "Cache-Control": "no-store"},
    )


@router.get("/logs/partial")
def logs_partial(
    request: Request,
    level: str = Query("ALL"),
    phase: str = Query(""),
    q: str = Query(""),
    _user=Depends(require_admin),
):
    level = level.upper() if level.upper() in _VALID_LEVELS else "ALL"
    rows = query_logs(level=level, phase=phase or None, search=q or None, limit=500)
    return request.app.state.template_response(
        request, "_logs_table.html", {"rows": rows},
    )
