"""Issued credentials view — list provisioned cards with install URL + QR.

For a PACS that receives credentials rather than supplying them, each row
also reports what actually reached it. That is the half of a Seos sync
nobody can otherwise see: the AccessGrid pass exists either way, and whether
its card numbers made it into the PACS is the difference between a pass that
opens doors and one that does not.
"""

from __future__ import annotations

import io
import logging

import segno
from fastapi import APIRouter, Depends, Request

from ..ag import AccessGridError, build_client
from ..auth import require_admin
from ..lib.pacs import build_adapter, get_descriptor
from ..settings_store import AccessGridConfig, PacsConfig
from ..sync import tracking
from ..sync.engine import derived_pacs_params

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/credentials")
def credentials_page(request: Request, _user=Depends(require_admin)):
    rows = [
        c for c in tracking.all_tracked()
        if c.ag_card_id and c.status != "deduped"
    ]
    written, pacs_name = _written_credentials()
    return request.app.state.template_response(
        request, "credentials.html",
        {
            "credentials": rows,
            # (person_id, credential_id) -> card numbers in the PACS.
            "written": written,
            "pacs_name": pacs_name,
        },
    )


def _written_credentials() -> tuple[dict[tuple[str, str], list[str]], str]:
    """What has reached the PACS, and what to call it. ({}, "") if not applicable."""
    pacs = PacsConfig.load() or {}
    vendor = pacs.get("vendor", "")
    descriptor = get_descriptor(vendor) if vendor else None
    if descriptor is None:
        return {}, ""
    try:
        # The direction is derived from the AccessGrid template, not stored,
        # so building from the raw params would get the read-only default
        # and report every pass as unwritten.
        ag_cfg = AccessGridConfig.load() or {}
        resolved = pacs
        if ag_cfg:
            client = build_client(ag_cfg["account_id"], ag_cfg["api_secret"])
            resolved = derived_pacs_params(client, ag_cfg, pacs)
        adapter = build_adapter(vendor, dict(resolved.get("params") or {}))
        if not getattr(adapter, "supports_credential_writeback", False):
            return {}, ""
        return adapter.written_credentials(), descriptor.display_name
    except Exception as e:  # noqa: BLE001 — a listing page must still render
        logger.warning("credentials: could not read PACS writeback state: %s", e)
        return {}, descriptor.display_name


@router.get("/credentials/{card_id}/install")
def credentials_install_partial(
    card_id: str, request: Request, _user=Depends(require_admin),
):
    """Fetch the install URL for a single card and render a QR code.

    HTMX target — each row in the table fires one of these in parallel
    on page load, so we don't block the initial render on N AG API calls.
    """
    ctx: dict[str, object] = {"card_id": card_id}
    ag = AccessGridConfig.load()
    if not ag:
        ctx["error"] = "AccessGrid not configured"
        return request.app.state.template_response(
            request, "_credentials_install.html", ctx,
        )

    try:
        client = build_client(ag["account_id"], ag["api_secret"])
        card = client.access_cards.get(card_id)
        url = getattr(card, "install_url", None) or getattr(card, "url", None)
    except AccessGridError as e:
        logger.warning("credentials: AG fetch failed for %s: %s", card_id, e)
        ctx["error"] = str(e)
        return request.app.state.template_response(
            request, "_credentials_install.html", ctx,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("credentials: unexpected error for %s: %s", card_id, e)
        ctx["error"] = f"{type(e).__name__}: {e}"
        return request.app.state.template_response(
            request, "_credentials_install.html", ctx,
        )

    if not url:
        ctx["error"] = "No install URL on this card yet"
        return request.app.state.template_response(
            request, "_credentials_install.html", ctx,
        )

    ctx["url"] = url
    ctx["qr_svg"] = _qr_svg(url)
    return request.app.state.template_response(
        request, "_credentials_install.html", ctx,
    )


def _qr_svg(url: str) -> str:
    qr = segno.make(url, error="m")
    buf = io.BytesIO()
    qr.save(buf, kind="svg", scale=3, border=2, xmldecl=False, svgns=True)
    return buf.getvalue().decode("utf-8")
