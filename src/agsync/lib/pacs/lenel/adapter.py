"""Lenel adapter — interface stub.

Selecting Lenel in the wizard reaches __init__'s factory which builds
this class. Every method raises NotImplementedError until someone
ports the lenel-onguard-service codebase into this adapter.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..base import (
    ConnectionResult,
    Credential,
    CredentialStatus,
    PacsDescriptor,
    Person,
)


class LenelAdapter:
    def __init__(self, **_params: Any) -> None:
        # Don't connect on construction so the wizard can build the
        # adapter and call test_connection() without raising.
        pass

    def descriptor(self) -> PacsDescriptor:
        from . import DESCRIPTOR
        return DESCRIPTOR

    def test_connection(self) -> ConnectionResult:
        return ConnectionResult(
            ok=False,
            message="Lenel adapter is not yet implemented in this build.",
        )

    def list_people(self) -> Iterable[Person]:
        raise NotImplementedError("Lenel adapter not implemented")

    def list_credentials(self, person_id: str) -> Iterable[Credential]:
        raise NotImplementedError("Lenel adapter not implemented")

    def update_credential_status(
        self, person_id: str, credential_id: str, status: CredentialStatus
    ) -> bool:
        raise NotImplementedError("Lenel adapter not implemented")

    @property
    def supports_status_writeback(self) -> bool:
        return False


HELP_TEXT: dict[str, dict[str, str]] = {
    "en": {
        "pacs.lenel.server": "SQL Server host",
        "pacs.lenel.database": "Database name",
        "pacs.lenel.username": "Username",
        "pacs.lenel.password": "Password",
        "pacs.lenel.trigger_help": (
            "Lenel adapter is not yet wired up in this build. Choose "
            "Avigilon for now."
        ),
    },
    "es": {
        "pacs.lenel.server": "Servidor SQL",
        "pacs.lenel.database": "Nombre de base de datos",
        "pacs.lenel.username": "Usuario",
        "pacs.lenel.password": "Contraseña",
        "pacs.lenel.trigger_help": (
            "El adaptador de Lenel todavía no está implementado en esta "
            "versión. Por ahora, elige Avigilon."
        ),
    },
}
