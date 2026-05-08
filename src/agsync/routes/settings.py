from __future__ import annotations

import socket

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from ..auth import require_admin
from ..config import get_settings
from ..settings_store import AccessGridConfig, PacsConfig

# Match what AccessGrid accepts for metadata keys: keep it conservative —
# letters, digits, underscore, hyphen — to avoid surprises in their API
# query syntax (`metadata[key]=value`).
_VALID_KEY_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)

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
    return request.app.state.template_response(
        request, "settings.html",
        {
            "ag_account": ag.get("account_id", ""),
            "ag_template": ag.get("template_id", ""),
            "ag_site_code": ag.get("site_code", ""),
            "ag_dedupe": bool(ag.get("dedupe_by_site_card", False)),
            "ag_extra_metadata": list(extras.items()),
            "ag_reserved_keys": sorted(AccessGridConfig.RESERVED_METADATA_KEYS),
            "pacs_vendor": pacs.get("vendor", ""),
            "pacs_params_keys": list((pacs.get("params") or {}).keys()),
            "db_path": str(s.db_path),
            "host_port": f"{s.host}:{s.port}",
            "ips": _local_ips(),
            "version": __import__("agsync").__version__,
            "ok": ok,
            "err": err,
            "err_key": err_key,
        },
    )


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
