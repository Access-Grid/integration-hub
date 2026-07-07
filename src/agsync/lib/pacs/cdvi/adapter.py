"""CDVI Atrium → vendor-agnostic adapter.

What makes CDVI different from the other adapters:

  * The enrollment trigger lives on the *card*, in its **Display Name**.
    A card is opted in when its display name contains the marker
    `[accessgrid]`, optionally with a wallet platform hint —
    `[accessgrid-apple]` or `[accessgrid-android]` (see TRIGGER_PATTERN).
    The trigger is evaluated per card, so an operator can enroll one of a
    user's cards without touching the others, and the marker doubles as a
    human-readable label in the Atrium UI.

  * CDVI cards don't store facility code + card number as separate
    fields — they store a single encoded hex `number` (8-bit site code
    in the high byte, 16-bit card number in the low two bytes; see
    `_decode_card_number`). We decode it back into
    Credential.site_code + Credential.card_number so phase 1 can dedupe
    and provision.

  * Cards are linked to users by a nested <USER> element on each card
    (card["USER"]["id"] once parsed), not a flat attribute. Unassigned
    cards carry <USER id="-1">. We fetch the card table once and group it
    by that user id, mirroring how the Alta adapter caches per-person
    state.

  * Atrium reuses object ids: a deleted card/user id is handed to the next
    one created. So a card's slot id alone is not a stable identifier. The
    credential id folds in the decoded site+card number (see
    _make_credential_id), and status writeback re-checks that identity
    against the slot's current occupant before writing, so a reused slot
    can never cause the wrong card to be suspended.

Shapes here match a live Atrium controller (firmware serial AA0089FE):
users.xml rows use `state` for the enable flag and carry no email/phone;
cards.xml rows use `en` and nest their assigned <USER>. Email lives in a
separate per-user SDK record (rec="cfg2", attribute email5), fetched only
for users with a trigger-active card since only those get provisioned.

This adapter never creates or updates CDVI **users** (a hard
requirement). It does support credential status writeback, but only by
flipping an existing **card**'s State (the `en` flag): suspend disables
the card, reactivate re-enables it. The card keeps its user, number, and
dates; no card is created or deleted, and no user record is ever written.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

from ..base import (
    ConnectionResult,
    Credential,
    CredentialStatus,
    PacsDescriptor,
    Person,
)
from .client import CdviClient

logger = logging.getLogger(__name__)

# Enrollment marker in a card's Display Name. Matches `[accessgrid]` and,
# optionally, a wallet platform hint: `[accessgrid-apple]` /
# `[accessgrid-android]`. Case-insensitive; matched anywhere in the name so
# operators can keep their own label text alongside it, e.g.
# "Amy iPhone [accessgrid-apple]". The captured group is the platform (or
# None when the bare marker is used).
TRIGGER_PATTERN = re.compile(r"\[accessgrid(?:-(apple|android))?\]", re.IGNORECASE)

# Candidate attribute keys, in priority order, for a card's display name.
# The exact key returned by cards.xml varies by controller/firmware, so we
# probe a few known spellings and take the first present.
CARD_NAME_KEYS = ("name", "_name", "label")


def _card_display_name(card: dict) -> str:
    for key in CARD_NAME_KEYS:
        if key in card and card[key] is not None:
            return str(card[key])
    return ""


def _trigger_platform(display_name: str) -> str | None:
    """Return the matched wallet platform, or None. Raises no match → None.

    Callers distinguish "no marker" from "bare marker" via the boolean of
    TRIGGER_PATTERN.search(); this only reports the captured platform.
    """
    m = TRIGGER_PATTERN.search(display_name)
    return (m.group(1).lower() if m and m.group(1) else None)


def _make_credential_id(slot_id: str, site_code: str, card_number: str) -> str:
    """Build a credential id that is stable to the *physical* card.

    Atrium reuses card slot ids after deletion (a deleted id is handed to the
    next created card), so a bare slot id is not a durable identifier. Folding
    the decoded site + card number into the id means a reused slot produces a
    different credential id — a new tracking row — instead of silently
    inheriting the deleted card's provisioning/state.
    """
    return f"{slot_id}:{site_code}:{card_number}"


def _parse_credential_id(credential_id: str) -> tuple[str, tuple[str, str] | None]:
    """Split a credential id into (slot_id, expected (site, card) or None)."""
    parts = str(credential_id).split(":")
    slot = parts[0]
    expected = (parts[1], parts[2]) if len(parts) >= 3 else None
    return slot, expected


def _decode_card_number(encoded: str) -> tuple[str, str]:
    """Reverse CDVI's `convert_card_data`: encoded hex → (site_code, card_number).

    The encoder packs an 8-bit site code and a 16-bit card number into a
    24-bit value rendered as hex. Returns strings ("", "") if the value
    can't be decoded.
    """
    if not encoded:
        return "", ""
    try:
        value = int(encoded, 16)
    except (ValueError, TypeError):
        return "", ""
    site_code = (value >> 16) & 0xFF
    card_number = value & 0xFFFF
    return str(site_code), str(card_number)


class CdviAdapter:
    def __init__(self, base_url: str, username: str, password: str):
        self._client = CdviClient(base_url=base_url, username=username, password=password)
        from . import DESCRIPTOR
        self._descriptor = DESCRIPTOR
        # person_id -> [card dict], lazily loaded on first list_credentials().
        self._cards_by_person: dict[str, list[dict]] | None = None
        # card id -> card dict, populated alongside _cards_by_person.
        self._card_by_id: dict[str, dict] = {}

    def descriptor(self) -> PacsDescriptor:
        return self._descriptor

    def test_connection(self) -> ConnectionResult:
        ok, msg = self._client.test_connection()
        return ConnectionResult(ok=ok, message=msg)

    def list_people(self) -> Iterable[Person]:
        # A fresh people listing invalidates the card cache.
        self._cards_by_person = None
        cards_by_person = self._ensure_cards_loaded()

        # Email is not in users.xml — it takes a separate per-user SDK read.
        # Only users with a trigger-active card ever get provisioned, so we
        # pay that read for those users alone (zero when nothing is enrolled).
        triggered_uids = {
            uid
            for uid, cards in cards_by_person.items()
            if any(TRIGGER_PATTERN.search(_card_display_name(c)) for c in cards)
        }

        for raw in self._client.list_users():
            pid = str(raw.get("id"))
            first = (raw.get("fn") or "").strip()
            last = (raw.get("ln") or "").strip()
            full_name = " ".join(p for p in (first, last) if p)
            email = self._client.get_user_email(pid) if pid in triggered_uids else ""
            yield Person(
                id=pid,
                full_name=full_name,
                first_name=first,
                last_name=last,
                # users.xml has no phone; email comes from the SDK cfg2 read.
                email=email,
                # User enable flag is `state` on users.xml (cards use `en`).
                active=(str(raw.get("state", "1")) == "1"),
                raw=raw,
            )

    def _ensure_cards_loaded(self) -> dict[str, list[dict]]:
        if self._cards_by_person is None:
            grouped: dict[str, list[dict]] = {}
            by_id: dict[str, dict] = {}
            for card in self._client.list_cards():
                by_id[str(card.get("id"))] = card
                # Each card nests its assigned <USER>; id == "-1" means
                # unassigned. There is no flat user_id attribute.
                user = card.get("USER")
                uid = str(user.get("id")) if isinstance(user, dict) else ""
                if uid and uid != "-1":
                    grouped.setdefault(uid, []).append(card)
            self._cards_by_person = grouped
            self._card_by_id = by_id
        return self._cards_by_person

    def list_credentials(self, person_id: str) -> Iterable[Credential]:
        pid = str(person_id)
        for raw in self._ensure_cards_loaded().get(pid, []):
            site_code, card_number = _decode_card_number(str(raw.get("number") or ""))

            # The trigger lives on the card's display name.
            display_name = _card_display_name(raw)
            trigger = TRIGGER_PATTERN.search(display_name) is not None
            if trigger:
                logger.debug(
                    "CDVI card %s enrolled via display name (platform=%s)",
                    raw.get("id"), _trigger_platform(display_name) or "unspecified",
                )

            # A card is active only when enabled and not flagged lost/stolen.
            enabled = str(raw.get("en", "1")) == "1"
            flagged = str(raw.get("lost", "0")) == "1" or str(raw.get("stolen", "0")) == "1"
            status = (
                CredentialStatus.ACTIVE
                if enabled and not flagged
                else CredentialStatus.SUSPENDED
            )

            yield Credential(
                id=_make_credential_id(str(raw.get("id")), site_code, card_number),
                person_id=pid,
                card_number=card_number,
                site_code=site_code,
                status=status,
                trigger_active=trigger,
                raw=raw,
            )

    def update_credential_status(
        self, person_id: str, credential_id: str, status: CredentialStatus
    ) -> bool:
        # Writeback touches CARDS only — CDVI users are never modified.
        # Suspend = disable the card's State; reactivate = re-enable it.
        # The card keeps its user/number/dates (no unassign, no delete).
        if status not in (CredentialStatus.ACTIVE, CredentialStatus.SUSPENDED):
            return False
        slot, expected = _parse_credential_id(credential_id)
        self._ensure_cards_loaded()
        card = self._card_by_id.get(slot)
        if card is None:
            return False
        # Atrium reuses slot ids, so confirm the card in this slot is still
        # the same physical card we tracked before flipping its state — never
        # suspend a different card that inherited a deleted one's id.
        if expected is not None and _decode_card_number(str(card.get("number") or "")) != expected:
            logger.warning(
                "CDVI: card slot %s now holds a different card than tracked "
                "(expected site/card %s) — skipping writeback (slot reuse)",
                slot, expected,
            )
            return False
        return self._client.set_card_enabled(
            card, enabled=(status == CredentialStatus.ACTIVE)
        )

    @property
    def supports_status_writeback(self) -> bool:
        return True


HELP_TEXT: dict[str, dict[str, str]] = {
    "en": {
        "pacs.cdvi.base_url": "Controller URL (e.g. https://192.168.1.50)",
        "pacs.cdvi.username": "Atrium username",
        "pacs.cdvi.password": "Atrium password",
        "pacs.cdvi.trigger_help": (
            "To enroll a card, open it in Atrium and put the marker "
            "'[accessgrid]' in its Display Name — optionally with a wallet "
            "hint, '[accessgrid-apple]' or '[accessgrid-android]'. You can "
            "keep your own label alongside it, e.g. 'Amy iPhone "
            "[accessgrid-apple]'. The card's encoded number is decoded into "
            "the AccessGrid site code and card number, and the sync engine "
            "provisions a pass for the card's assigned user on the next "
            "cycle. This integration never creates or modifies CDVI users; "
            "the only write it performs is toggling an existing card's "
            "State (active/inactive) to suspend or reactivate a credential."
        ),
    },
    "es": {
        "pacs.cdvi.base_url": "URL del controlador (p. ej. https://192.168.1.50)",
        "pacs.cdvi.username": "Usuario de Atrium",
        "pacs.cdvi.password": "Contraseña de Atrium",
        "pacs.cdvi.trigger_help": (
            "Para inscribir una tarjeta, ábrala en Atrium y coloque el "
            "marcador '[accessgrid]' en su Nombre para mostrar (Display "
            "Name), opcionalmente con una pista de billetera: "
            "'[accessgrid-apple]' o '[accessgrid-android]'. Puede mantener "
            "su propia etiqueta junto al marcador, p. ej. 'Amy iPhone "
            "[accessgrid-apple]'. El número codificado de la tarjeta se "
            "decodifica en el código de sitio y el número de tarjeta de "
            "AccessGrid, y el motor de sincronización aprovisiona un pase "
            "para el usuario asignado a la tarjeta en el próximo ciclo. "
            "Esta integración nunca crea ni modifica usuarios de CDVI; la "
            "única escritura que realiza es cambiar el estado (State) de "
            "una tarjeta existente (activa/inactiva) para suspender o "
            "reactivar una credencial."
        ),
    },
}
