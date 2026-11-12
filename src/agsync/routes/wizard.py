"""Setup wizard.

Five steps:
  1. Create admin user (only if no admin exists yet)
  2. AccessGrid creds + test
  3. Choose PACS
  4. PACS creds + test
  5. Done — start the engine

A PACS that advertises `requires_connect` splits step 4 in two, because
nothing about it can be reached until a human has signed in: first the
connection settings, then an AG Connect hand-off followed by the trigger
choice, which is read live from the PACS. The engine is only started once
that trigger exists — a half-configured install would otherwise start
syncing against no trigger at all.

We don't persist multi-step state in a session; each step submits the
data it needs and the next step is rendered. Refreshing a step is safe.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse

from ..ag import PROTOCOL_SEOS, template_shape
from ..ag import build_client as build_ag_client
from ..auth import SESSION_COOKIE, current_user, sign_session
from ..auth.store import admin_exists, create_admin
from ..connect import register as uri_register
from ..lib.pacs import available_pacs, build_adapter, get_descriptor
from ..settings_store import AccessGridConfig, PacsConfig, is_configured
from ..sync import get_engine

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/wizard")

# Bootstrap license key. Required to create the first admin so that a
# LAN attacker who reaches the freshly-installed NUC cannot race the
# legitimate operator to claim the admin account. Replace this value
# per deployment before building the binary.
BOOTSTRAP_LICENSE_KEY = "AGSYNC-REPLACE-WITH-PER-DEPLOYMENT-LICENSE-KEY"


def _license_key_valid(supplied: str) -> bool:
    return hmac.compare_digest(
        supplied.strip().encode("utf-8"),
        BOOTSTRAP_LICENSE_KEY.encode("utf-8"),
    )


def _require_admin_if_bootstrapped(request: Request) -> None:
    if not admin_exists():
        return
    if current_user(request) is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"Location": "/login"},
        )


def _needs_connect_step(pacs: dict | None) -> bool:
    """True when a connect-gated PACS is saved but has no trigger yet."""
    if not pacs:
        return False
    descriptor = get_descriptor(pacs.get("vendor", ""))
    if descriptor is None or not descriptor.requires_connect:
        return False
    return not (pacs.get("params") or {}).get("trigger_card_format")


def _needs_card_format(pacs: dict | None) -> bool:
    """True when a PACS whose card format is picked in setup has none yet."""
    descriptor = get_descriptor((pacs or {}).get("vendor", ""))
    return bool(descriptor and descriptor.picks_card_format and not (pacs.get("params") or {}).get("card_format"))


def _step(request: Request) -> int:
    if not admin_exists():
        return 1
    if AccessGridConfig.load() is None:
        return 2
    pacs = PacsConfig.load()
    if pacs is None:
        # If they got partway through PACS step we route based on whether
        # a vendor has been picked yet via the form param.
        return 3
    if _needs_connect_step(pacs) or _needs_card_format(pacs):
        return 4
    return 5


@router.get("")
def wizard_index(request: Request):
    s = _step(request)
    if s == 4:
        url = "/wizard/card-format" if _needs_card_format(PacsConfig.load()) else "/wizard/connect"
        return RedirectResponse(url=url, status_code=303)
    return request.app.state.template_response(
        request, f"wizard/step{s}.html",
        {"step": s, "pacs_options": available_pacs()},
    )


@router.post("/admin")
def wizard_admin(
    request: Request,
    license_key: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    confirm: str = Form(...),
):
    if admin_exists():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin is already configured",
        )
    if not _license_key_valid(license_key):
        return request.app.state.template_response(
            request, "wizard/step1.html",
            {"step": 1, "error": "Invalid license key."},
        )
    if password != confirm or not username.strip() or len(password) < 8:
        return request.app.state.template_response(
            request, "wizard/step1.html",
            {"step": 1, "error": "Passwords must match and be at least 8 characters."},
        )
    create_admin(username.strip(), password)
    response = RedirectResponse(url="/wizard", status_code=303)
    response.set_cookie(
        key=SESSION_COOKIE, value=sign_session(username.strip()),
        max_age=3600, httponly=True, secure=True, samesite="lax",
    )
    return response


@router.post("/accessgrid")
def wizard_ag(
    request: Request,
    account_id: str = Form(...),
    api_secret: str = Form(...),
    template_id: str = Form(...),
    site_code: str = Form(...),
):
    _require_admin_if_bootstrapped(request)
    site_code = site_code.strip()
    if not site_code.isdigit():
        return request.app.state.template_response(
            request, "wizard/step2.html",
            {"step": 2, "error": "Site code must be a non-negative integer."},
        )
    AccessGridConfig.save(
        account_id.strip(), api_secret.strip(), template_id.strip(), site_code,
    )
    return RedirectResponse(url="/wizard", status_code=303)


@router.post("/pacs-vendor")
def wizard_pacs_vendor(request: Request, vendor: str = Form(...)):
    _require_admin_if_bootstrapped(request)
    descriptor = get_descriptor(vendor)
    if descriptor is None:
        return RedirectResponse(url="/wizard", status_code=303)
    return request.app.state.template_response(
        request, "wizard/step4.html",
        {"step": 4, "vendor": vendor, "descriptor": descriptor},
    )


@router.post("/pacs")
async def wizard_pacs(request: Request):
    _require_admin_if_bootstrapped(request)
    form = await request.form()
    vendor = form.get("vendor")
    if not vendor:
        return RedirectResponse(url="/wizard", status_code=303)
    descriptor = get_descriptor(vendor)
    if descriptor is None:
        return RedirectResponse(url="/wizard", status_code=303)
    params = {f.id: form.get(f.id, "") for f in descriptor.connection_fields}
    options: dict = {}
    if descriptor.supports_file_data:
        enc = form.get("credential_encoding", "site_card")
        options["credential_encoding"] = "file_data" if enc == "file_data" else "site_card"
    PacsConfig.save(vendor, params, options)
    if descriptor.requires_connect:
        # Nothing works until a human signs in, so hand off to AG Connect
        # rather than starting the engine against an unusable connection.
        return RedirectResponse(url="/wizard/connect", status_code=303)
    if descriptor.picks_card_format:
        # Read from the PACS just saved, so it can only be asked now.
        return RedirectResponse(url="/wizard/card-format", status_code=303)
    # Setup is complete — start the engine.
    if is_configured():
        get_engine().start()
    return RedirectResponse(url="/wizard", status_code=303)


def _seos_confirmation_needed(descriptor) -> bool:
    """Must the operator confirm the Seos facility code before finishing?

    For a PACS whose direction follows the AccessGrid template, when that
    template is Seos — or cannot be read, so might be. AccessGrid assigns a
    Seos pass's facility code, and the token the sync writes holds only the
    card number: Unity matches the facility code against its own card
    formats, so without one carrying it the token opens no door, silently.
    Nothing readable says which code AccessGrid assigns, so a human checks.
    """
    if not descriptor.derives_mode_from_template:
        return False
    ag_cfg = AccessGridConfig.load()
    if not ag_cfg:
        return True
    try:
        client = build_ag_client(ag_cfg["account_id"], ag_cfg["api_secret"])
        protocol, _ = template_shape(client, ag_cfg["template_id"])
    except Exception as e:  # noqa: BLE001 — unknown is asked about, not guessed
        logger.warning("wizard: could not read the card template: %s", e)
        protocol = ""
    return not protocol or protocol == PROTOCOL_SEOS


@router.get("/card-format")
def wizard_card_format(request: Request):
    """Step 4b — the PACS's card formats, read live, to pick the site's."""
    _require_admin_if_bootstrapped(request)
    pacs = PacsConfig.load()
    descriptor = get_descriptor((pacs or {}).get("vendor", ""))
    if descriptor is None or not descriptor.picks_card_format:
        return RedirectResponse(url="/wizard", status_code=303)
    formats: list[dict] = []
    error = ""
    try:
        adapter = build_adapter(pacs["vendor"], pacs.get("params") or {})
        formats = [{"id": fid, "label": label} for fid, label in adapter.card_formats()]
    except Exception as e:  # noqa: BLE001 — surfaced to the operator verbatim
        logger.warning("wizard: could not read card formats: %s", e)
        error = f"{type(e).__name__}: {e}"
    return request.app.state.template_response(
        request, "wizard/step4_format.html",
        {"step": 4, "descriptor": descriptor, "formats": formats, "error": error,
         "seos_confirm": _seos_confirmation_needed(descriptor),
         "seos_confirm_missing": request.query_params.get("confirm") == "missing"},
    )


@router.post("/card-format")
def wizard_card_format_save(request: Request, card_format: str = Form(...),
                            seos_facility_code_confirmed: str = Form("")):
    """Record the site's card format, completing setup."""
    _require_admin_if_bootstrapped(request)
    pacs = PacsConfig.load()
    if not pacs:
        return RedirectResponse(url="/wizard", status_code=303)
    descriptor = get_descriptor(pacs.get("vendor", ""))
    if descriptor and seos_facility_code_confirmed != "yes" and _seos_confirmation_needed(descriptor):
        logger.warning("wizard: setup not finished — the Seos facility code was not confirmed")
        return RedirectResponse(url="/wizard/card-format?confirm=missing", status_code=303)
    try:
        adapter = build_adapter(pacs["vendor"], pacs.get("params") or {})
        params = adapter.card_format_params(dict(pacs.get("params") or {}), card_format)
    except Exception as e:  # noqa: BLE001 — back to the picker, which reads the formats again
        logger.warning("wizard: could not record the card format: %s", e)
        return RedirectResponse(url="/wizard/card-format", status_code=303)
    PacsConfig.save(pacs["vendor"], params, pacs.get("options") or {})
    engine = get_engine()
    engine.invalidate_pacs_adapter()
    if is_configured():
        engine.start()
    return RedirectResponse(url="/wizard", status_code=303)


@router.get("/connect")
def wizard_connect(request: Request):
    """Step 4b — hand off to AG Connect, then pick the trigger card format."""
    _require_admin_if_bootstrapped(request)
    pacs = PacsConfig.load()
    if not pacs:
        return RedirectResponse(url="/wizard", status_code=303)
    descriptor = get_descriptor(pacs.get("vendor", ""))
    if descriptor is None or not descriptor.requires_connect:
        return RedirectResponse(url="/wizard", status_code=303)

    from .connect import _login_url, _registry

    login_url = _login_url()
    launch_id = _registry.create(login_url).launch_id if login_url else ""
    return request.app.state.template_response(
        request, "wizard/step4_connect.html",
        {
            "step": 4,
            "vendor": descriptor.vendor,
            "descriptor": descriptor,
            "launch_id": launch_id,
            "uri_registered": uri_register.is_registered(),
        },
    )


@router.post("/pacs-trigger")
def wizard_pacs_trigger(
    request: Request,
    trigger_card_format: str = Form(...),
):
    """Record the enrollment trigger, completing a connect-gated setup.

    The direction of travel is not asked for: the engine reads it from the
    AccessGrid card template's protocol on every cycle.
    """
    _require_admin_if_bootstrapped(request)
    pacs = PacsConfig.load()
    if not pacs:
        return RedirectResponse(url="/wizard", status_code=303)
    from .connect import trigger_params

    try:
        params = trigger_params(dict(pacs.get("params") or {}), trigger_card_format)
    except Exception as e:  # noqa: BLE001 — back to the picker, which shows the read error
        logger.warning("wizard: could not record the trigger format: %s", e)
        return RedirectResponse(url="/wizard", status_code=303)
    PacsConfig.save(pacs["vendor"], params, pacs.get("options") or {})
    engine = get_engine()
    engine.invalidate_pacs_adapter()
    if is_configured():
        engine.start()
    return RedirectResponse(url="/wizard", status_code=303)
