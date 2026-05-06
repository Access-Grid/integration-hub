"""Avigilon → vendor-agnostic adapter.

Maps the Avigilon-specific token + identity dicts returned by
AvigilonClient into the engine's Person/Credential model. The trigger
field for Avigilon is `embossed_number == "accessgrid"` (case
insensitive) — that's encoded here, not in the engine.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from ..base import (
    ConnectionResult,
    Credential,
    CredentialStatus,
    PacsAdapter,
    PacsDescriptor,
    Person,
)
from .client import AvigilonClient

TRIGGER_VALUE = "accessgrid"

_AVIGILON_STATUS_TO_CREDENTIAL: dict[str, CredentialStatus] = {
    "1": CredentialStatus.ACTIVE,
    "2": CredentialStatus.SUSPENDED,
    "3": CredentialStatus.SUSPENDED,
    "4": CredentialStatus.SUSPENDED,
}

_CREDENTIAL_TO_AVIGILON_STATUS: dict[CredentialStatus, str] = {
    CredentialStatus.ACTIVE: "1",
    CredentialStatus.SUSPENDED: "2",
}


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class AvigilonAdapter:
    def __init__(self, host: str, username: str, password: str, verify_ssl: bool = False):
        self._client = AvigilonClient(host=host, username=username, password=password, verify_ssl=verify_ssl)
        from . import DESCRIPTOR
        self._descriptor = DESCRIPTOR

    def descriptor(self) -> PacsDescriptor:
        return self._descriptor

    def test_connection(self) -> ConnectionResult:
        ok, msg = self._client.test_connection()
        return ConnectionResult(ok=ok, message=msg)

    def list_people(self) -> Iterable[Person]:
        for raw in self._client.get_all_identities():
            yield Person(
                id=raw["id"],
                full_name=raw.get("full_name", ""),
                first_name=raw.get("first_name", ""),
                last_name=raw.get("last_name", ""),
                email=raw.get("email", ""),
                phone=raw.get("phone") or raw.get("work_phone", ""),
                title=raw.get("title", ""),
                department=raw.get("department", ""),
                active=(raw.get("status", "1") == "1"),
                raw=raw,
            )

    def list_credentials(self, person_id: str) -> Iterable[Credential]:
        for raw in self._client.get_identity_tokens(person_id):
            embossed = (raw.get("embossed_number") or "").lower()
            yield Credential(
                id=raw["id"],
                person_id=person_id,
                card_number=raw.get("internal_number") or raw.get("embossed_number") or "",
                status=_AVIGILON_STATUS_TO_CREDENTIAL.get(raw.get("status", "1"), CredentialStatus.UNKNOWN),
                activate_date=_parse_iso(raw.get("activate_date", "")),
                deactivate_date=_parse_iso(raw.get("deactivate_date", "")),
                trigger_active=(embossed == TRIGGER_VALUE),
                raw=raw,
            )

    def update_credential_status(
        self, person_id: str, credential_id: str, status: CredentialStatus
    ) -> bool:
        avig_status = _CREDENTIAL_TO_AVIGILON_STATUS.get(status)
        if avig_status is None:
            return False
        # Avigilon writes are full PUTs — fetch the current token row first
        # so we don't blank out other fields.
        tokens = self._client.get_identity_tokens(person_id)
        current = next((t for t in tokens if t["id"] == credential_id), None)
        if current is None:
            return False
        return self._client.update_token_status(
            person_id, credential_id, avig_status, current_token_data=current
        )

    @property
    def supports_status_writeback(self) -> bool:
        return True


# Add localized help text. Stored as a flat dict on the package so the
# i18n loader can merge in per-locale strings without an adapter knowing
# anything about the JSON files.
HELP_TEXT: dict[str, dict[str, str]] = {
    "en": {
        "pacs.avigilon.host": "Host (IP or hostname)",
        "pacs.avigilon.username": "Username",
        "pacs.avigilon.password": "Password",
        "pacs.avigilon.trigger_help": (
            "To enroll an identity, set the token's Embossed Number field to "
            "the literal text 'accessgrid'. Put the actual card number in "
            "the Internal Number field. The sync engine will pick up the "
            "token on the next cycle and provision an AccessGrid pass for "
            "that person."
        ),
    },
    "es": {
        "pacs.avigilon.host": "Host (IP o nombre)",
        "pacs.avigilon.username": "Usuario",
        "pacs.avigilon.password": "Contraseña",
        "pacs.avigilon.trigger_help": (
            "Para inscribir una identidad, configure el campo 'Número en "
            "relieve' (Embossed Number) del token con el texto literal "
            "'accessgrid'. Coloque el número de tarjeta real en el campo "
            "'Número interno' (Internal Number). El motor de sincronización "
            "tomará el token en el próximo ciclo y aprovisionará un pase "
            "de AccessGrid para esa persona."
        ),
    },
}
