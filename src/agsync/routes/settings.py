from __future__ import annotations

import logging
import re
import socket
from urllib.parse import urlparse, urlunparse

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from ..ag import test_connection as ag_test
from ..auth import require_admin
from ..config import get_settings
from ..lib.pacs import build_adapter, get_descriptor
from ..settings_store import (
    AccessGridConfig,
    NotificationConfig,
    PacsConfig,
    PacsSession,
)
from ..sync import get_engine

# Match what AccessGrid accepts for metadata keys: keep it conservative —
# letters, digits, underscore, hyphen — to avoid surprises in their API
# query syntax (`metadata[key]=value`).
_VALID_KEY_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _local_ips() -> list[str]:
    out: set[str] = set()
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None):
            family, *_, sockaddr = info
            if family == socket.AF_INET:
                ip = sockaddr[0]
                if not ip.startswith("127."):
                    out.add(ip)
    except OSError:
        pass
    return sorted(out)


def _address_fields(descriptor) -> list:
    """The connection fields that say *where* the PACS is.

    Only `url` kind. Two reasons, both deliberate: no field of that kind is
    a credential, so nothing secret can be rendered back into the settings
    page, and it leaves alone the text fields that already have sections of
    their own. A vendor whose address is a bare host or server name does
    not appear here yet.
    """
    if descriptor is None:
        return []
    return [f for f in descriptor.connection_fields if f.kind == "url"]


@router.get("/settings")
def settings_page(
    request: Request,
    ok: str = "",
    err: str = "",
    err_key: str = "",
    _user=Depends(require_admin),
):
    s = get_settings()
    ag = AccessGridConfig.load() or {}
    pacs = PacsConfig.load() or {}
    extras: dict[str, str] = ag.get("extra_metadata") or {}
    vendor_id = pacs.get("vendor", "")
    descriptor = get_descriptor(vendor_id) if vendor_id else None
    params = pacs.get("params") or {}
    smtp = NotificationConfig.load() or {}
    session = PacsSession.load(vendor_id) or {}
    return request.app.state.template_response(
        request, "settings.html",
        {
            "ag_account": ag.get("account_id", ""),
            "ag_template": ag.get("template_id", ""),
            "ag_site_code": ag.get("site_code", ""),
            "ag_dedupe": bool(ag.get("dedupe_by_site_card", False)),
            "ag_extra_metadata": list(extras.items()),
            "ag_card_title": ag.get("card_title", ""),
            "ag_card_classification": ag.get(
                "card_classification", AccessGridConfig.DEFAULT_CARD_CLASSIFICATION,
            ),
            "ag_reserved_keys": sorted(AccessGridConfig.RESERVED_METADATA_KEYS),
            "pacs_vendor": descriptor.display_name if descriptor else vendor_id,
            "pacs_params_keys": list((pacs.get("params") or {}).keys()),
            "pacs_requires_connect": bool(descriptor and descriptor.requires_connect),
            # The trigger is a live value read from the PACS, so the section
            # only appears for adapters that can enumerate one.
            "pacs_has_trigger_formats": "trigger_card_format" in params,
            # Only shown for a PACS that synthesizes addresses because it
            # stores none of its own.
            # Whichever address fields this adapter declares, with what is
            # saved for each. Driven by the descriptor so a new PACS needs
            # no change here, and narrowed to url-kind fields so a
            # credential can never be rendered back into the page.
            "pacs_address_fields": [
                {
                    "id": f.id,
                    "label_key": f.label_key,
                    "placeholder": f.placeholder,
                    "value": params.get(f.id, ""),
                }
                for f in _address_fields(descriptor)
            ],
            "pacs_email_domain": params.get("email_domain"),
            "pacs_notify_email": params.get("notify_email", ""),
            "pacs_session_at": session.get("captured_at", "") if session.get("auth_cookie") else "",
            "smtp_host": smtp.get("smtp_host", ""),
            "smtp_port": smtp.get("smtp_port", 587),
            "smtp_from": smtp.get("from_address", ""),
            "smtp_username": smtp.get("smtp_username", ""),
            "smtp_use_ssl": bool(smtp.get("use_ssl", False)),
            "db_path": str(s.db_path),
            "host_port": f"{s.host}:{s.port}",
            "ips": _local_ips(),
            "version": __import__("agsync").__version__,
            "ok": ok,
            "err": err,
            "err_key": err_key,
        },
    )


@router.post("/settings/accessgrid")
def update_accessgrid(
    request: Request,
    account_id: str = Form(...),
    template_id: str = Form(...),
    api_secret: str = Form(""),
    _user=Depends(require_admin),
):
    """Change the AccessGrid account, template or API key.

    Proven before it is stored. Saving credentials that do not work would
    stop every phase at once, and there is no way back through this page —
    the engine would be unable to read the template it needs, which is the
    trap this route exists to remove rather than to reproduce.

    A blank key means "keep the stored one", so the account or template can
    be corrected on its own.
    """
    account_id, template_id = account_id.strip(), template_id.strip()
    api_secret = api_secret.strip()
    if not account_id or not template_id:
        return RedirectResponse(url="/settings?err=accessgrid", status_code=303)

    current = AccessGridConfig.load() or {}
    if not current:
        return RedirectResponse(url="/settings?err=not_configured", status_code=303)

    ok, message = ag_test(account_id, api_secret or current.get("api_secret", ""), template_id)
    if not ok:
        logger.warning("Rejected AccessGrid credentials: %s", message)
        return RedirectResponse(url="/settings?err=accessgrid_rejected", status_code=303)

    if not AccessGridConfig.update_credentials(account_id, api_secret, template_id):
        return RedirectResponse(url="/settings?err=not_configured", status_code=303)

    logger.info(
        "AccessGrid credentials changed — account %s, template %s%s",
        account_id, template_id, ", new API key" if api_secret else "",
    )
    engine = get_engine()
    engine.invalidate_pacs_adapter()
    engine.trigger_now()
    return RedirectResponse(url="/settings?ok=accessgrid", status_code=303)


@router.post("/settings/site-code")
def update_site_code(
    request: Request,
    site_code: str = Form(...),
    _user=Depends(require_admin),
):
    site_code = site_code.strip()
    if not site_code.isdigit():
        return RedirectResponse(url="/settings?err=site_code", status_code=303)
    if not AccessGridConfig.update_site_code(site_code):
        return RedirectResponse(url="/settings?err=not_configured", status_code=303)
    return RedirectResponse(url="/settings?ok=site_code", status_code=303)


@router.post("/settings/dedupe")
def update_dedupe(
    request: Request,
    enabled: str = Form(""),
    _user=Depends(require_admin),
):
    flag = enabled.strip().lower() in ("1", "on", "true", "yes")
    if not AccessGridConfig.update_dedupe(flag):
        return RedirectResponse(url="/settings?err=not_configured", status_code=303)
    return RedirectResponse(url="/settings?ok=dedupe", status_code=303)


@router.post("/settings/email-domain")
def update_email_domain(
    request: Request,
    email_domain: str = Form(...),
    _user=Depends(require_admin),
):
    """Change the domain used for synthesized cardholder addresses.

    This rewrites every address the PACS side derives, so phase 6 will push
    the new ones to AccessGrid on the next cycle. That is the intended
    behaviour, but it touches every pass already issued.
    """
    domain = email_domain.strip().lstrip("@")
    if not domain or " " in domain or "." not in domain:
        return RedirectResponse(url="/settings?err=email_domain", status_code=303)
    if not PacsConfig.update_params(email_domain=domain):
        return RedirectResponse(url="/settings?err=not_configured", status_code=303)
    logger.info("Synthesized email domain changed to %r", domain)
    engine = get_engine()
    engine.invalidate_pacs_adapter()
    engine.trigger_now()
    return RedirectResponse(url="/settings?ok=email_domain", status_code=303)


@router.post("/settings/pacs-address")
async def update_pacs_address(
    request: Request,
    _user=Depends(require_admin),
):
    """Point this install at a different address for the same PACS.

    Until now the address could only be set while running the wizard, so a
    server that moved — or one mistyped at setup — meant setting the
    integration up again from scratch.

    Changing it invalidates the captured session. The auth cookie was
    issued by the old host and means nothing to the new one, and keeping it
    would be the worst of the options: requests would simply look
    unauthenticated, which reads downstream as an expired session rather
    than as a changed address. So the session is cleared and the operator
    is asked to sign in again, which for a captcha-gated PACS they have to
    do by hand anyway.
    """
    pacs = PacsConfig.load() or {}
    vendor = pacs.get("vendor", "")
    descriptor = get_descriptor(vendor) if vendor else None
    fields = {f.id: f for f in _address_fields(descriptor)}
    if not fields:
        return RedirectResponse(url="/settings?err=not_configured", status_code=303)

    form = await request.form()
    changes: dict[str, str] = {}
    for field_id in fields:
        raw = str(form.get(field_id, "")).strip()
        normalised = _normalise_url(raw)
        if normalised is None:
            return RedirectResponse(url="/settings?err=pacs_address", status_code=303)
        changes[field_id] = normalised

    current = pacs.get("params") or {}
    if all(current.get(k, "") == v for k, v in changes.items()):
        # Saving the same address should not cost the operator a reconnect.
        return RedirectResponse(url="/settings?ok=pacs_address", status_code=303)

    if not PacsConfig.update_params(**changes):
        return RedirectResponse(url="/settings?err=not_configured", status_code=303)

    # Logged without the old value alongside it: this is the one setting
    # whose history tells somebody which server used to be trusted.
    logger.info("PACS address changed to %r", changes)
    PacsSession.clear(vendor)
    engine = get_engine()
    engine.invalidate_pacs_adapter()
    return RedirectResponse(url="/settings?ok=pacs_address", status_code=303)


# Hostnames, dotted IPv4, and the bracketless form of an IPv6 literal that
# `hostname` hands back. Deliberately not a full RFC check: it is here to
# refuse what is obviously not an address, not to adjudicate DNS.
_HOSTISH = re.compile(r"[A-Za-z0-9._:\-]+")


def _normalise_url(raw: str) -> str | None:
    """A reachable http(s) origin, or None if it is not one.

    Returned without a trailing slash because the adapters join paths onto
    it directly, and `//Account/LogIn` is not the same path.
    """
    if not raw or " " in raw:
        return None
    # Typing the host alone is the common case, so a missing scheme is
    # filled in rather than refused. It is filled in before parsing, which
    # is why the port is then checked: "javascript:alert(1)" has no "://",
    # so it would become "https://javascript:alert(1)" and parse as a host
    # with a nonsense port rather than being rejected.
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    try:
        _ = parsed.port  # raises when the port is not a number
    except ValueError:
        return None
    host = parsed.hostname or ""
    if not host or not _HOSTISH.fullmatch(host):
        return None
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))


@router.post("/settings/card-fields")
def update_card_fields(
    request: Request,
    card_title: str = Form(""),
    card_classification: str = Form(""),
    _user=Depends(require_admin),
):
    """Set the title and classification stamped on every pass issued."""
    if not AccessGridConfig.update_card_fields(
        card_title.strip(), card_classification.strip(),
    ):
        return RedirectResponse(url="/settings?err=not_configured", status_code=303)
    return RedirectResponse(url="/settings?ok=card_fields", status_code=303)


@router.get("/settings/trigger-format")
def trigger_format_partial(request: Request, _user=Depends(require_admin)):
    """The enrollment-trigger picker, refreshed from the PACS on each load.

    Formats are read live rather than remembered, so a format added or
    renamed in the PACS shows up here without re-running setup.
    """
    pacs = PacsConfig.load() or {}
    params = pacs.get("params") or {}
    current = str(params.get("trigger_card_format") or "")
    formats: list[dict] = []
    error = ""
    try:
        adapter = build_adapter(pacs["vendor"], params)
        formats = [{"id": fid, "label": label} for fid, label in adapter.card_formats()]
    except Exception as e:  # noqa: BLE001 — surfaced to the operator verbatim
        logger.warning("settings: could not read card formats: %s", e)
        error = f"{type(e).__name__}: {e}"
    return request.app.state.template_response(
        request, "_settings_trigger.html",
        {
            "formats": formats,
            "format_ids": [f["id"] for f in formats],
            "current": current,
            "error": error,
        },
    )


@router.post("/settings/trigger-format")
def update_trigger_format(
    request: Request,
    trigger_card_format: str = Form(...),
    _user=Depends(require_admin),
):
    """Change which card format enrolls cardholders.

    Narrowing the trigger un-enrolls everyone holding the old format, and
    phase 2 will terminate their passes on the next cycle — that is the
    intended way to stop provisioning, but it is not a small change.
    """
    pacs = PacsConfig.load()
    if not pacs:
        return RedirectResponse(url="/settings?err=not_configured", status_code=303)
    params = dict(pacs.get("params") or {})
    previous = params.get("trigger_card_format")
    params["trigger_card_format"] = trigger_card_format.strip()
    PacsConfig.save(pacs["vendor"], params, pacs.get("options") or {})
    logger.info(
        "Enrollment trigger changed from card format %r to %r",
        previous, params["trigger_card_format"],
    )
    engine = get_engine()
    engine.invalidate_pacs_adapter()
    engine.trigger_now()
    return RedirectResponse(url="/settings?ok=trigger", status_code=303)


@router.post("/settings/notifications")
def update_notifications(
    request: Request,
    smtp_host: str = Form(""),
    smtp_port: str = Form("587"),
    from_address: str = Form(""),
    smtp_username: str = Form(""),
    smtp_password: str = Form(""),
    use_ssl: str = Form(""),
    _user=Depends(require_admin),
):
    """Save the optional SMTP relay used for reconnect notices.

    An empty password field means "keep the stored one", so the form can be
    re-submitted without the secret ever being rendered back to the browser.
    """
    existing = NotificationConfig.load() or {}
    try:
        port = int(smtp_port or 587)
    except ValueError:
        return RedirectResponse(url="/settings?err=smtp_port", status_code=303)
    ssl_on = use_ssl.strip().lower() in ("1", "on", "true", "yes")
    NotificationConfig.save(
        smtp_host=smtp_host.strip(),
        smtp_port=port,
        smtp_username=smtp_username.strip(),
        smtp_password=smtp_password or existing.get("smtp_password", ""),
        from_address=from_address.strip(),
        # STARTTLS is the default for everything except implicit-SSL relays.
        use_starttls=not ssl_on,
        use_ssl=ssl_on,
    )
    return RedirectResponse(url="/settings?ok=notifications", status_code=303)


def _validate_meta_key(key: str) -> str:
    """Return '' if valid, else an error code suitable for the URL."""
    if not key:
        return "meta_empty_key"
    if key in AccessGridConfig.RESERVED_METADATA_KEYS:
        return "meta_reserved"
    if any(c not in _VALID_KEY_CHARS for c in key):
        return "meta_bad_chars"
    if len(key) > 64:
        return "meta_too_long"
    return ""


@router.post("/settings/extra-metadata")
async def update_extra_metadata(
    request: Request,
    _user=Depends(require_admin),
):
    """Replace the extra_metadata dict from a form submission.

    Form is parallel-list: each row submits a `meta_key` and a `meta_value`.
    Empty rows (both fields blank) are silently dropped — that's how the UI
    handles deletion. Last value wins on duplicate keys.
    """
    form = await request.form()
    keys = form.getlist("meta_key")
    values = form.getlist("meta_value")

    pairs: dict[str, str] = {}
    for raw_key, raw_value in zip(keys, values, strict=False):
        key = (raw_key or "").strip()
        value = (raw_value or "").strip()
        if not key and not value:
            continue
        err = _validate_meta_key(key)
        if err:
            return RedirectResponse(
                url=f"/settings?err={err}&err_key={key}", status_code=303,
            )
        pairs[key] = value

    if not AccessGridConfig.update_extra_metadata(pairs):
        return RedirectResponse(url="/settings?err=not_configured", status_code=303)
    return RedirectResponse(url="/settings?ok=extra_metadata", status_code=303)
