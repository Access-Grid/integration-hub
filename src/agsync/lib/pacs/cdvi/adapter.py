"""CDVI Atrium → vendor-agnostic adapter.

What makes CDVI different from the other adapters:

  * The enrollment trigger lives on the *user*, via the Atrium **Custom
    Fields** feature. The operator adds a custom text field (in the
    reference tenant it's labelled "Uses AccessGrid", backed by the
    Atrium `customLarge1` slot) and sets it on the users they want
    synced. Any non-empty / affirmative value opts that user in — every
    card assigned to them is then synced. We cache each user's custom
    field value during list_people() so list_credentials() can gate on
    it without a second round-trip.

  * CDVI cards don't store facility code + card number as separate
    fields — they store a single encoded hex `number` (8-bit site code
    in the high byte, 16-bit card number in the low two bytes; see
    `_decode_card_number`). We decode it back into
    Credential.site_code + Credential.card_number so phase 1 can dedupe
    and provision.

  * Cards are linked to users by the card's `user_id`. We fetch the card
    table once and group it by user_id, mirroring how the Alta adapter
    caches externalId.

This adapter is **read-only**: it never creates or updates CDVI users
(a hard requirement) and does no card writeback either — status
writeback is advertised as unsupported.
"""

from __future__ import annotations

import logging
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

# Candidate attribute keys, in priority order, for the Atrium custom-large-1
# slot that carries the "Uses AccessGrid" flag. The exact key returned by
# users.xml varies by controller/firmware, so we probe a few known spellings
# and take the first present. Confirm against a live users.xml if enrollment
# isn't picked up.
CUSTOM_FIELD_KEYS = (
    "custom_large1",
    "customLarge1",
    "cust_large1",
    "custom_large_1",
    "customlarge1",
)

# Values that explicitly mean "not enrolled" even though the field is set.
NEGATIVE_VALUES = {"", "0", "no", "false", "n", "off", "none"}


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


def _trigger_value(user: dict) -> str:
    for key in CUSTOM_FIELD_KEYS:
        if key in user and user[key] is not None:
            return str(user[key]).strip()
    return ""


def _is_triggered(value: str) -> bool:
    return value.strip().lower() not in NEGATIVE_VALUES


class CdviAdapter:
    def __init__(self, base_url: str, username: str, password: str):
        self._client = CdviClient(base_url=base_url, username=username, password=password)
        from . import DESCRIPTOR
        self._descriptor = DESCRIPTOR
        # person_id -> bool, populated by list_people().
        self._triggered_by_person: dict[str, bool] = {}
        # person_id -> [card dict], lazily loaded on first list_credentials().
        self._cards_by_person: dict[str, list[dict]] | None = None

    def descriptor(self) -> PacsDescriptor:
        return self._descriptor

    def test_connection(self) -> ConnectionResult:
        ok, msg = self._client.test_connection()
        return ConnectionResult(ok=ok, message=msg)

    def list_people(self) -> Iterable[Person]:
        # A fresh people listing invalidates the card cache.
        self._cards_by_person = None
        for raw in self._client.list_users():
            pid = str(raw.get("id"))
            self._triggered_by_person[pid] = _is_triggered(_trigger_value(raw))

            first = (raw.get("fn") or "").strip()
            last = (raw.get("ln") or "").strip()
            full_name = " ".join(p for p in (first, last) if p)
            yield Person(
                id=pid,
                full_name=full_name,
                first_name=first,
                last_name=last,
                email=(raw.get("email") or "").strip(),
                active=(str(raw.get("en", "1")) == "1"),
                raw=raw,
            )

    def _ensure_cards_loaded(self) -> dict[str, list[dict]]:
        if self._cards_by_person is None:
            grouped: dict[str, list[dict]] = {}
            for card in self._client.list_cards():
                uid = str(card.get("user_id") or "")
                if uid:
                    grouped.setdefault(uid, []).append(card)
            self._cards_by_person = grouped
        return self._cards_by_person

    def list_credentials(self, person_id: str) -> Iterable[Credential]:
        pid = str(person_id)
        trigger = self._triggered_by_person.get(pid, False)
        for raw in self._ensure_cards_loaded().get(pid, []):
            site_code, card_number = _decode_card_number(str(raw.get("number") or ""))

            # A card is active only when enabled and not flagged lost/stolen.
            enabled = str(raw.get("en", "1")) == "1"
            flagged = str(raw.get("lost", "0")) == "1" or str(raw.get("stolen", "0")) == "1"
            status = (
                CredentialStatus.ACTIVE
                if enabled and not flagged
                else CredentialStatus.SUSPENDED
            )

            yield Credential(
                id=str(raw.get("id")),
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
        # Read-only integration: we never write back to the CDVI directory.
        return False

    @property
    def supports_status_writeback(self) -> bool:
        return False


HELP_TEXT: dict[str, dict[str, str]] = {
    "en": {
        "pacs.cdvi.base_url": "Controller URL (e.g. https://192.168.1.50)",
        "pacs.cdvi.username": "Atrium username",
        "pacs.cdvi.password": "Atrium password",
        "pacs.cdvi.trigger_help": (
            "To enroll a user, open the user in Atrium, expand Custom "
            "Fields, and set the 'Uses AccessGrid' field to any value (for "
            "example 'yes'). Every card assigned to that user is then "
            "synced — the card's encoded number is decoded into the "
            "AccessGrid site code and card number. The sync engine picks "
            "the user up on the next cycle and provisions an AccessGrid "
            "pass. This integration never creates or modifies CDVI users."
        ),
    },
    "es": {
        "pacs.cdvi.base_url": "URL del controlador (p. ej. https://192.168.1.50)",
        "pacs.cdvi.username": "Usuario de Atrium",
        "pacs.cdvi.password": "Contraseña de Atrium",
        "pacs.cdvi.trigger_help": (
            "Para inscribir a un usuario, ábralo en Atrium, expanda "
            "'Custom Fields' (Campos personalizados) y establezca el campo "
            "'Uses AccessGrid' con cualquier valor (por ejemplo 'yes'). "
            "Se sincronizará cada tarjeta asignada a ese usuario: el número "
            "codificado de la tarjeta se decodifica en el código de sitio y "
            "el número de tarjeta de AccessGrid. El motor de sincronización "
            "tomará al usuario en el próximo ciclo y aprovisionará un pase "
            "de AccessGrid. Esta integración nunca crea ni modifica "
            "usuarios de CDVI."
        ),
    },
}
