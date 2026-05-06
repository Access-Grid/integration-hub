"""Public API routes — health and connection-test endpoints used by the
wizard via HTMX."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request

from ..ag import test_connection as ag_test
from ..auth import current_user
from ..lib.pacs import build_adapter

router = APIRouter(prefix="/api")


@router.get("/health")
def health():
    return {"status": "ok", "service": "ag-sync-tool"}


@router.post("/test-ag")
def test_ag(
    request: Request,
    account_id: str = Form(...),
    api_secret: str = Form(...),
    template_id: str = Form(...),
):
    if not _request_allowed(request):
        return {"ok": False, "message": "Unauthorized"}
    ok, msg = ag_test(account_id.strip(), api_secret.strip(), template_id.strip())
    return _result_payload(request, ok, msg)


@router.post("/test-pacs")
async def test_pacs(request: Request):
    if not _request_allowed(request):
        return {"ok": False, "message": "Unauthorized"}
    form = await request.form()
    vendor = form.get("vendor", "")
    params = {k: v for k, v in form.items() if k != "vendor"}
    try:
        adapter = build_adapter(vendor, params)
        result = adapter.test_connection()
        return _result_payload(request, result.ok, result.message)
    except Exception as e:  # noqa: BLE001
        return _result_payload(request, False, f"{type(e).__name__}: {e}")


def _result_payload(request: Request, ok: bool, message: str) -> dict:
    # Wizard test buttons use HTMX which displays the returned HTML
    # snippet directly. Return {ok, message} plus a server-rendered HTML
    # blob in `html` so the template can either show a JSON badge or
    # render the snippet.
    from ..i18n import get_translator

    t = get_translator(request.cookies.get("agsync_lang") or "en").t
    text = t("test.ok") if ok else t("test.failed", error=message)
    css = "ok" if ok else "err"
    html = f'<div class="test-result {css}">{text}</div>'
    return {"ok": ok, "message": text, "html": html}


def _request_allowed(request: Request) -> bool:
    # The wizard runs before login, so allow unauthenticated access during
    # initial setup (no AG creds yet means setup is incomplete).
    from ..settings_store import is_configured

    if not is_configured():
        return True
    return current_user(request) is not None
