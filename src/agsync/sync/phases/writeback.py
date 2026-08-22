"""Hand AccessGrid-allocated credentials back to a PACS that receives them.

Used by the Seos direction, where AccessGrid mints the facility code and
card number and the PACS is the destination rather than the source. Two
things call in here: phase 1 the moment a pass is provisioned, and phase 4
every cycle afterwards — because a pass installed on a second device (a
watch alongside a phone) gains a second credential long after provisioning,
and that one has to reach the PACS too.

Adapters are required to be idempotent about this, so re-offering the full
list every cycle is the intended usage rather than a wasteful one.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from ...lib.pacs import CredentialIdentity, PacsAdapter

logger = logging.getLogger(__name__)

# Where an AccessGrid pass exposes more than one credential.
#
# `details` is the important one: issuing against a card template *pair*
# returns a unified pass whose identities are one per platform (an Apple
# card and an Android card, each with its own card number) and whose own
# site_code/card_number are absent. Reading only the top level finds nothing
# there, and the pass looks like it has no credential at all.
#
# The others are probed because the per-device surface is still growing;
# today they carry no card numbers, so the card's own identity is the
# fallback for a single-template issue.
_DEVICE_COLLECTIONS = ("details", "device_credentials", "devices", "credentials")
_SITE_KEYS = ("site_code", "facility_code", "siteCode")
_CARD_KEYS = ("card_number", "cardNumber", "number")


def _as_datetime(value: Any) -> datetime | None:
    """Coerce whatever AccessGrid returned for a date into a datetime.

    The API sends ISO strings ("2027-08-22T05:07:55.521Z"), but the SDK does
    not parse them, so a CredentialIdentity built straight from a card would
    carry a str where its annotation promises a datetime — and the PACS
    adapter, reasonably, does date arithmetic on it.
    """
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            logger.debug("Unparseable AccessGrid date %r", value)
    return None


def _attr(obj: Any, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)
        if value not in (None, ""):
            return str(value)
    return ""


def identities_from_card(card: Any) -> list[CredentialIdentity]:
    """Every credential AccessGrid has allocated for one pass.

    A pass issued against a card template pair carries one per platform; a
    single-template issue carries the card's own identity. Both end up as a
    credential the PACS has to hold, which is why a cardholder needs two
    free slots before Seos will provision.
    """
    activate = _as_datetime(getattr(card, "start_date", None))
    expire = _as_datetime(getattr(card, "expiration_date", None))

    out: list[CredentialIdentity] = []
    seen: set[tuple[str, str]] = set()
    for collection in _DEVICE_COLLECTIONS:
        for entry in getattr(card, collection, None) or []:
            site, number = _attr(entry, _SITE_KEYS), _attr(entry, _CARD_KEYS)
            if number and (site, number) not in seen:
                seen.add((site, number))
                out.append(
                    CredentialIdentity(
                        site,
                        number,
                        activate,
                        # Each half of a pair carries its own dates.
                        _as_datetime(_attr(entry, ("expiration_date",))) or expire,
                    )
                )

    site, number = _attr(card, _SITE_KEYS), _attr(card, _CARD_KEYS)
    if number and (site, number) not in seen:
        out.append(CredentialIdentity(site, number, activate, expire))
    return out


def identities_from_cards(cards: Any) -> list[CredentialIdentity]:
    """Union the identities across every card belonging to one issue.

    Provisioning hands back a single object, but a later listing returns a
    card template pair as two separate cards. Both shapes have to produce
    the same set, or a re-offer would look like a credential went missing.
    """
    if not isinstance(cards, list | tuple):
        cards = [cards]
    out: list[CredentialIdentity] = []
    seen: set[tuple[str, str]] = set()
    for card in cards:
        for identity in identities_from_card(card):
            key = (str(identity.site_code), str(identity.card_number))
            if key not in seen:
                seen.add(key)
                out.append(identity)
    return out


def push_allocated_identities(
    pacs: PacsAdapter, person_id: str, credential_id: str, card: Any
) -> bool:
    """Write an issue's AccessGrid-allocated identities into the PACS.

    `card` may be one pass or the list of cards a later listing returned for
    it; both describe the same issue.
    """
    if not getattr(pacs, "supports_credential_writeback", False):
        return False
    identities = identities_from_cards(card)
    if not identities:
        logger.warning(
            "  AG pass for %s/%s exposes no credential yet — nothing to write back",
            person_id, credential_id,
        )
        return False
    try:
        return bool(pacs.write_back_credentials(person_id, credential_id, identities))
    except Exception as e:  # noqa: BLE001 — one bad cardholder must not stop the cycle
        logger.error(
            "  Failed to write credentials back for %s/%s: %s",
            person_id, credential_id, e,
        )
        return False
