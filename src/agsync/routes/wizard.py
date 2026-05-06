"""Setup wizard.

Five steps:
  1. Create admin user (only if no admin exists yet)
  2. AccessGrid creds + test
  3. Choose PACS
  4. PACS creds + test
  5. Done — start the engine

We don't persist multi-step state in a session; each step submits the
data it needs and the next step is rendered. Refreshing a step is safe.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from ..auth import SESSION_COOKIE, sign_session
from ..auth.store import admin_exists, create_admin
from ..lib.pacs import available_pacs, get_descriptor
from ..settings_store import AccessGridConfig, PacsConfig, is_configured
from ..sync import get_engine

router = APIRouter(prefix="/wizard")


def _step(request: Request) -> int:
    if not admin_exists():
        return 1
    if AccessGridConfig.load() is None:
        return 2
    if PacsConfig.load() is None:
        # If they got partway through PACS step we route based on whether
        # a vendor has been picked yet via the form param.
        return 3
    return 5


@router.get("")
def wizard_index(request: Request):
    s = _step(request)
    return request.app.state.template_response(
        request, f"wizard/step{s}.html",
        {"step": s, "pacs_options": available_pacs()},
    )


@router.post("/admin")
def wizard_admin(request: Request, username: str = Form(...), password: str = Form(...), confirm: str = Form(...)):
    if password != confirm or not username.strip() or len(password) < 8:
        return request.app.state.template_response(
            request, "wizard/step1.html",
            {"step": 1, "error": "Passwords must match and be at least 8 characters."},
        )
    create_admin(username.strip(), password)
    response = RedirectResponse(url="/wizard", status_code=303)
    response.set_cookie(
        key=SESSION_COOKIE, value=sign_session(username.strip()),
        max_age=3600, httponly=True, samesite="lax",
    )
    return response


@router.post("/accessgrid")
def wizard_ag(
    request: Request,
    account_id: str = Form(...),
    api_secret: str = Form(...),
    template_id: str = Form(...),
):
    AccessGridConfig.save(account_id.strip(), api_secret.strip(), template_id.strip())
    return RedirectResponse(url="/wizard", status_code=303)


@router.post("/pacs-vendor")
def wizard_pacs_vendor(request: Request, vendor: str = Form(...)):
    descriptor = get_descriptor(vendor)
    if descriptor is None:
        return RedirectResponse(url="/wizard", status_code=303)
    return request.app.state.template_response(
        request, "wizard/step4.html",
        {"step": 4, "vendor": vendor, "descriptor": descriptor},
    )


@router.post("/pacs")
async def wizard_pacs(request: Request):
    form = await request.form()
    vendor = form.get("vendor")
    if not vendor:
        return RedirectResponse(url="/wizard", status_code=303)
    descriptor = get_descriptor(vendor)
    if descriptor is None:
        return RedirectResponse(url="/wizard", status_code=303)
    params = {f.id: form.get(f.id, "") for f in descriptor.connection_fields}
    PacsConfig.save(vendor, params)
    # Setup is complete — start the engine.
    if is_configured():
        get_engine().start()
    return RedirectResponse(url="/wizard", status_code=303)
