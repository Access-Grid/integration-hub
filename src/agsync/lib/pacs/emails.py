"""Cardholder email addresses: which ones are usable, and a made-up one when none is.

Shared by the adapters whose PACS may hold no usable address: Millennium,
whose email fields are free text, and Avigilon Unity, whose Seos passes
AccessGrid will not issue without one.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


def first_valid_email(*candidates: str) -> str:
    """The first of these that could actually be delivered to.

    PACS email fields can hold whatever was typed — a name, a note, a phone
    number. Issuing a pass to one of those fails at AccessGrid with an error
    about the address rather than about the field it came from, so anything
    without an @ and a dot after it is treated as absent.
    """
    for candidate in candidates:
        value = (candidate or "").strip()
        if not value or " " in value:
            continue
        local, _, domain = value.partition("@")
        if local and "." in domain:
            return value
    return ""


def synthesize_email(first: str, last: str, employee_id: str, domain: str) -> str:
    """first.last.<employee id>@domain, for a cardholder with no address.

    The id keeps two people with the same name apart. "" when there is no
    domain to use or no id to keep them apart with.
    """
    if not employee_id:
        logger.error("PACS: cannot synthesize an email for %s %s — no Employee ID", first, last)
        return ""
    domain = (domain or "").strip().lstrip("@")
    if not domain:
        return ""
    parts = [re.sub(r"[^a-z0-9]", "", (p or "").lower()) for p in (first, last)]
    local = ".".join([p for p in parts if p] + [str(employee_id)])
    return f"{local}@{domain}"
