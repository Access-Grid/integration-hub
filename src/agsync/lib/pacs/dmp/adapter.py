"""DMP System Link → vendor-agnostic adapter.

What makes DMP different from the other adapters:

  * **Read-only.** DMP System Link is store-and-forward: editing the DBISAM
    database does not reach the panel without an operator-run `Panel > Send`,
    and the panel requires keypad authorization we can't automate. So this
    adapter never writes back — `supports_status_writeback` is False and the
    engine's AG→PACS phases are no-ops for DMP. Revocation is handled entirely
    on the AccessGrid side (suspend the pass); DMP stays the read source.

  * **No email in the panel database.** DMP user records carry no email or
    phone. AccessGrid needs a delivery channel, so the operator puts each
    person's email in a DMP **User Field** (`U_FIELD1` by default). That field
    doubles as the enrollment trigger: a user is synced iff it has an email
    there. This avoids a name-marker (the `NAME` field truncates at 16 chars)
    and needs no external mapping file.

  * **Card number = the `CODE` field.** DMP stores the extracted card number as
    a string. It does not retain the facility/site code, so `site_code` is left
    empty and phase 1 applies the globally-configured site code. `PROFILE1-4`
    hold the user's access levels.

  * **Recycled user numbers.** DMP reuses `USER_NUM` slots over time, so we key
    the person and credential on the stable `CODE` (physical card identity), not
    the slot. Live-vs-deleted row filtering is handled in the client.
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
from .client import DmpClient, DmpError

logger = logging.getLogger(__name__)

DEFAULT_EMAIL_FIELD = "U_FIELD1"


class DmpAdapter:
    def __init__(
        self,
        db_path: str,
        encryption_key: str,
        email_field: str = DEFAULT_EMAIL_FIELD,
    ) -> None:
        self._client = DmpClient(db_path=db_path, encryption_key=encryption_key)
        self._email_field = (email_field or DEFAULT_EMAIL_FIELD).strip().upper()
        # code -> user row, populated by list_people(), reused by list_credentials().
        self._by_code: dict[str, dict] = {}

    def descriptor(self) -> PacsDescriptor:
        from . import DESCRIPTOR
        return DESCRIPTOR

    def test_connection(self) -> ConnectionResult:
        ok, msg = self._client.test_connection()
        return ConnectionResult(ok=ok, message=msg)

    def _email(self, row: dict) -> str:
        return str(row.get(self._email_field) or "").strip()

    def _load(self) -> dict[str, dict]:
        by_code: dict[str, dict] = {}
        try:
            for row in self._client.read_users():
                by_code[str(row["CODE"]).strip()] = row
        except DmpError as e:
            logger.error("DMP: failed to read users: %s", e)
            raise
        self._by_code = by_code
        return by_code

    def list_people(self) -> Iterable[Person]:
        for code, row in self._load().items():
            yield Person(
                id=code,
                full_name=str(row.get("NAME") or "").strip(),
                email=self._email(row),
                # DMP stores no phone.
                active=bool(row.get("ACTIVE")),
                department=str(row.get("DEPARTMENT") or "").strip(),
                raw=row,
            )

    def list_credentials(self, person_id: str) -> Iterable[Credential]:
        code = str(person_id).strip()
        row = (self._by_code or self._load()).get(code)
        if row is None:
            return
        email = self._email(row)
        yield Credential(
            id=code,
            person_id=code,
            card_number=code,
            # DMP discards the facility code; phase 1 applies the global one.
            site_code="",
            status=(
                CredentialStatus.ACTIVE
                if row.get("ACTIVE")
                else CredentialStatus.SUSPENDED
            ),
            # Enrollment trigger: an email in the configured User Field.
            trigger_active=bool(email),
            raw=row,
        )

    def update_credential_status(
        self, person_id: str, credential_id: str, status: CredentialStatus
    ) -> bool:
        # Read-only: DMP is store-and-forward and the panel can't be written
        # unattended. Revocation happens on the AccessGrid side.
        return False

    @property
    def supports_status_writeback(self) -> bool:
        return False


HELP_TEXT: dict[str, dict[str, str]] = {
    "en": {
        "pacs.dmp.db_path": "DMP database folder",
        "pacs.dmp.encryption_key": "DMP encryption key",
        "pacs.dmp.email_field": "Email User Field (default U_FIELD1)",
        "pacs.dmp.trigger_help": (
            "DMP is read-only. To enroll a user, put their email address in a "
            "DMP User Field (User Field 1 by default) in System Link — that "
            "field is both how AccessGrid reaches them and the enrollment "
            "trigger. The card number comes from the user's Code. Revocations "
            "are handled in AccessGrid; the tool never writes back to DMP."
        ),
    },
    "es": {
        "pacs.dmp.db_path": "Carpeta de base de datos DMP",
        "pacs.dmp.encryption_key": "Clave de cifrado DMP",
        "pacs.dmp.email_field": "Campo de usuario para el correo (predet. U_FIELD1)",
        "pacs.dmp.trigger_help": (
            "DMP es de solo lectura. Para inscribir a un usuario, escribe su "
            "correo electrónico en un Campo de Usuario de DMP (Campo 1 por "
            "defecto) en System Link: ese campo es tanto la forma en que "
            "AccessGrid lo contacta como el disparador de inscripción. El "
            "número de tarjeta proviene del Código del usuario. Las "
            "revocaciones se gestionan en AccessGrid; la herramienta nunca "
            "escribe de vuelta en DMP."
        ),
    },
}
