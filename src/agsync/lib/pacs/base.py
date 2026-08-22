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

Most PACS are a source of credentials: the card exists there first and we
copy it out. Millennium Ultra in Seos mode is the other direction —
AccessGrid mints the facility code and card number and the adapter writes
them back. Those adapters set `Credential.allocate_identity` so phase 1
sends no identity of its own, advertise `supports_credential_writeback`,
and receive the result as `CredentialIdentity` values.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Protocol


class PacsAuthExpired(RuntimeError):
    """The PACS session we were given is no longer valid.

    Distinct from an ordinary connection error because no amount of retrying
    fixes it — a human has to sign in again. The engine stops the cycle,
    flags the UI, and notifies the operator instead of letting an empty
    result look like "every cardholder was deleted".
    """


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
    # Per-credential site/facility code. When set, phase 1 prefers this over
    # the global site_code in settings (used by Avigilon Alta, whose facility
    # code is entered per-credential rather than configured once).
    site_code: str = ""
    # True when AccessGrid should mint the credential's identity instead of
    # the PACS supplying one. Phase 1 then omits site_code/card_number
    # entirely (AccessGrid allocates on omission) and skips dedupe, since
    # there is nothing yet to dedupe against. Set by adapters that write
    # credentials *into* the PACS — see supports_credential_writeback.
    allocate_identity: bool = False
    # Pre-encoded credential payload (hex), when the PACS stores one. CDVI
    # keeps a single encoded card `number`; set this to that value so an
    # operator can choose to transmit it verbatim as AccessGrid `file_data`
    # instead of the decoded site_code + card_number. Empty when the vendor
    # has no such blob (Avigilon, Alta).
    file_data: str = ""
    status: CredentialStatus = CredentialStatus.UNKNOWN
    activate_date: datetime | None = None
    deactivate_date: datetime | None = None
    trigger_active: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CredentialIdentity:
    """An AccessGrid-allocated credential, on its way back into a PACS.

    One per issued device: a pass installed on both a phone and a watch
    yields two, each with its own card number.
    """

    site_code: str
    card_number: str
    activate_date: datetime | None = None
    deactivate_date: datetime | None = None


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
    connection_fields: list[ConnectionField]
    # True if this PACS exposes a pre-encoded credential payload that can be
    # sent to AccessGrid as `file_data`. Gates the wizard's encoding radio;
    # when False the operator only gets site_code + card_number.
    supports_file_data: bool = False
    # True if the operator must hand us a browser session before the
    # integration can talk to the PACS at all (Millennium Ultra's login is
    # captcha-gated). The wizard then runs the AG Connect step, and only
    # afterwards can it read live values such as the card-format list.
    requires_connect: bool = False
    # True if the adapter's direction of travel is a property of the
    # AccessGrid card template rather than something to ask the operator.
    # The engine then resolves the template's protocol each cycle and passes
    # it in as `mode`, so the two systems cannot drift apart.
    derives_mode_from_template: bool = False


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

    # Optional: adapters whose PACS *receives* credentials from AccessGrid.
    # When True, phase 1 hands back the identities AccessGrid allocated so
    # the adapter can write them into the PACS, and phase 4 re-offers any
    # that appear later (a second device installed after provisioning).
    @property
    def supports_credential_writeback(self) -> bool: ...

    def write_back_credentials(
        self,
        person_id: str,
        credential_id: str,
        identities: list[CredentialIdentity],
    ) -> bool:
        """Write AccessGrid-allocated identities into the PACS.

        Must be idempotent: it is re-offered every cycle with the full list,
        and identities already present are expected to be skipped.
        """
