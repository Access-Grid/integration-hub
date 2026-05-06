"""PACS adapter contract.

The sync engine talks only to this interface — it never imports anything
from a vendor-specific package. New PACS support = drop a new package
under lib/pacs/<vendor>/ that implements PacsAdapter and registers
itself in registry.py.

The two domain models, Person and Credential, are deliberately small.
Each adapter is responsible for normalizing whatever shape its PACS
returns into these fields. Anything vendor-specific goes in `raw`.

Crucially, each Credential carries `trigger_active: bool` already
populated by the adapter — the engine never knows which field on which
object the operator must set to "accessgrid" (or whatever the local
sentinel is). That mapping is owned by the adapter alongside the help
text shown in the wizard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Iterable, Protocol


class CredentialStatus(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Person:
    id: str
    full_name: str
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    title: str = ""
    department: str = ""
    active: bool = True
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Credential:
    id: str
    person_id: str
    card_number: str = ""
    status: CredentialStatus = CredentialStatus.UNKNOWN
    activate_date: datetime | None = None
    deactivate_date: datetime | None = None
    trigger_active: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConnectionResult:
    ok: bool
    message: str = ""


@dataclass(frozen=True)
class PacsDescriptor:
    """Static metadata about a PACS, surfaced in the setup wizard."""

    vendor: str  # machine id, e.g. "avigilon"
    display_name: str  # human-friendly, e.g. "Avigilon Unity (Plasec)"
    # i18n keys for help text shown in the wizard
    trigger_help_key: str
    # Connection-form schema: list of (field_id, label_key, type, required)
    connection_fields: list["ConnectionField"]


@dataclass(frozen=True)
class ConnectionField:
    id: str
    label_key: str
    kind: str = "text"  # text | password | url
    required: bool = True
    placeholder: str = ""


class PacsAdapter(Protocol):
    """Vendor-agnostic interface the sync engine calls into."""

    def descriptor(self) -> PacsDescriptor: ...

    def test_connection(self) -> ConnectionResult: ...

    def list_people(self) -> Iterable[Person]: ...

    def list_credentials(self, person_id: str) -> Iterable[Credential]: ...

    def update_credential_status(
        self, person_id: str, credential_id: str, status: CredentialStatus
    ) -> bool:
        """Write a status change back to the PACS. Return True on success."""

    # Capability flags. Default True; adapters can advertise less.
    @property
    def supports_status_writeback(self) -> bool: ...
