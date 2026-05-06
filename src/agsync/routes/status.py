from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from ..auth import require_admin
from ..sync import get_engine

router = APIRouter()


@router.get("/status")
def status_page(request: Request, _user=Depends(require_admin)):
    engine = get_engine()
    return request.app.state.template_response(
        request, "status.html", {"engine_status": engine.get_status()},
    )


@router.post("/sync/run")
def sync_run(request: Request, _user=Depends(require_admin)):
    get_engine().trigger_now()
    return RedirectResponse(url="/status", status_code=303)


@router.get("/status/partial")
def status_partial(request: Request, _user=Depends(require_admin)):
    """HTMX target: just the inner status card so the page can poll."""
    return request.app.state.template_response(
        request, "_status_card.html",
        {"engine_status": get_engine().get_status()},
    )
