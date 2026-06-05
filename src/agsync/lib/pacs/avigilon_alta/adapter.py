"""Avigilon Alta Access → vendor-agnostic adapter.

Two things make Alta different from the on-prem Avigilon Unity adapter:

  * The enrollment trigger lives on the *user*, not the credential. A
    user is opted in by setting their `externalId` to "accessgrid"; every
    one of that user's credentials is then synced. We cache each person's
    externalId during list_people() so list_credentials() can read it
    without an extra round-trip.

  * The AccessGrid site code comes from the *credential's* facility code
    (the value the operator typed into the Alta credential form), not the
    global site_code in settings. The adapter surfaces it on
    Credential.site_code; phase 1 prefers that over the settings value.

Only Wiegand ID cards (credentialType id 2) are synced — those are the
ones that carry a facilityCode / cardId pair.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from ..base import (
    ConnectionResult,
    Credential,
    CredentialStatus,
    PacsDescriptor,
    Person,
)
from .client import AltaClient, _parse_iso

TRIGGER_VALUE = "accessgrid"

# credentialType.id for "Card: Wiegand ID" — the only type we sync.
WIEGAND_CREDENTIAL_TYPE_ID = 2


class AvigilonAltaAdapter:
    def __init__(self, email: str, password: str):
        self._client = AltaClient(email=email, password=password)
        from . import DESCRIPTOR
        self._descriptor = DESCRIPTOR
        # person_id -> externalId (lowercased), populated by list_people().
        self._external_id_by_person: dict[str, str] = {}

    def descriptor(self) -> PacsDescriptor:
        return self._descriptor

    def test_connection(self) -> ConnectionResult:
        ok, msg = self._client.test_connection()
        return ConnectionResult(ok=ok, message=msg)

    def list_people(self) -> Iterable[Person]:
        for raw in self._client.list_users():
            pid = str(raw.get("id"))
            identity = raw.get("identity") or {}
            external_id = (raw.get("externalId") or "").strip().lower()
            self._external_id_by_person[pid] = external_id

            full_name = (identity.get("fullName") or "").strip()
            yield Person(
                id=pid,
                full_name=full_name,
                first_name=identity.get("firstName") or "",
                last_name=identity.get("lastName") or "",
                email=identity.get("email") or "",
                phone=identity.get("mobilePhone") or "",
                title=raw.get("title") or "",
                department=raw.get("department") or "",
                active=(raw.get("status") == "A"),
                raw=raw,
            )

    def list_credentials(self, person_id: str) -> Iterable[Credential]:
        trigger = self._external_id_by_person.get(str(person_id), "") == TRIGGER_VALUE
        for raw in self._client.list_credentials(person_id):
            cred_type = (raw.get("credentialType") or {}).get("id")
            if cred_type != WIEGAND_CREDENTIAL_TYPE_ID:
                continue

            card = raw.get("card") or {}
            fields = card.get("fields") or {}
            facility_code = str(fields.get("facilityCode") or card.get("facilityCode") or "")
            card_id = str(fields.get("cardId") or card.get("cardId") or "")

            end_date = _parse_iso(raw.get("endDate"))
            now = datetime.now(UTC)
            status = (
                CredentialStatus.SUSPENDED
                if end_date is not None and end_date <= now
                else CredentialStatus.ACTIVE
            )

            yield Credential(
                id=str(raw.get("id")),
                person_id=str(person_id),
                card_number=card_id,
                site_code=facility_code,
                status=status,
                activate_date=_parse_iso(raw.get("startDate")),
                deactivate_date=end_date,
                trigger_active=trigger,
                raw=raw,
            )

    def update_credential_status(
        self, person_id: str, credential_id: str, status: CredentialStatus
    ) -> bool:
        # Re-fetch the credential so we echo its card block back on the
        # PATCH (Alta blanks the card otherwise) and preserve startDate.
        current = next(
            (
                c for c in self._client.list_credentials(person_id)
                if str(c.get("id")) == str(credential_id)
            ),
            None,
        )
        if current is None:
            return False

        card = current.get("card") or {}
        card_format = card.get("cardFormat") or {}
        card_number = card.get("number") or ""
        card_format_id = card_format.get("id") or card.get("cardFormatId")
        if not card_number or card_format_id is None:
            return False

        now = datetime.now(UTC)
        if status == CredentialStatus.SUSPENDED:
            end_date = now.isoformat()
        elif status == CredentialStatus.ACTIVE:
            # Reactivate by pushing the window a year out.
            end_date = (now + timedelta(days=365)).isoformat()
        else:
            return False

        return self._client.patch_credential_dates(
            person_id,
            credential_id,
            start_date=current.get("startDate"),
            end_date=end_date,
            card_number=str(card_number),
            card_format_id=int(card_format_id),
            is_output_enabled=bool(card.get("isOutputEnabled", False)),
        )

    @property
    def supports_status_writeback(self) -> bool:
        return True


HELP_TEXT: dict[str, dict[str, str]] = {
    "en": {
        "pacs.avigilon_alta.email": "Login email",
        "pacs.avigilon_alta.password": "Password",
        "pacs.avigilon_alta.trigger_help": (
            "To enroll a user, set their External ID field to the literal "
            "text 'accessgrid' in Avigilon Alta. Every Wiegand card "
            "credential on that user is then synced — the credential's "
            "Facility Code becomes the AccessGrid site code and its Card "
            "Number becomes the card number. The sync engine picks the user "
            "up on the next cycle and provisions an AccessGrid pass."
        ),
    },
    "es": {
        "pacs.avigilon_alta.email": "Correo de acceso",
        "pacs.avigilon_alta.password": "Contraseña",
        "pacs.avigilon_alta.trigger_help": (
            "Para inscribir a un usuario, configure su campo 'External ID' "
            "(ID externo) con el texto literal 'accessgrid' en Avigilon "
            "Alta. Se sincronizará cada credencial de tarjeta Wiegand de ese "
            "usuario: el código de instalación (Facility Code) de la "
            "credencial se convierte en el código de sitio de AccessGrid y "
            "su número de tarjeta en el número de tarjeta. El motor de "
            "sincronización tomará al usuario en el próximo ciclo y "
            "aprovisionará un pase de AccessGrid."
        ),
    },
}
