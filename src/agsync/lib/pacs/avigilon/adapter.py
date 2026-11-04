"""Avigilon Unity → vendor-agnostic adapter.

The trigger is per token: Embossed Number set to `accessgrid` (any case).
The card number is the token's Internal Number — never the Embossed Number,
which by then holds the trigger word.

All of a cycle's Unity reads happen in list_people(). The identity list has
no email, phone or title, so for anyone holding a trigger token we fetch
their detail too, and their tokens are fetched anyway to know who that is.
list_credentials() then answers from what was read, and says plainly when a
person was not read — a failed read is never passed on as "no tokens".
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from ..base import (
    ConnectionResult,
    Credential,
    CredentialStatus,
    PacsDescriptor,
    PacsRecordUnavailable,
    Person,
)
from .client import UnityClient, UnityError

logger = logging.getLogger(__name__)

TRIGGER_VALUE = "accessgrid"

_TOKEN_STATUS: dict[str, CredentialStatus] = {
    "1": CredentialStatus.ACTIVE,
    "2": CredentialStatus.SUSPENDED,  # inactive
    "3": CredentialStatus.SUSPENDED,  # not yet active
    "4": CredentialStatus.SUSPENDED,  # expired
}
_WRITABLE_STATUS: dict[CredentialStatus, str] = {
    CredentialStatus.ACTIVE: "1",
    CredentialStatus.SUSPENDED: "2",
}

# Unity's date format has not been seen on a live box yet. ISO is tried
# first; the rest are the shapes a Rails app on an LDAP store (`dn=cn=…,
# dc=plasec`) plausibly sends. A date matching none of them holds the
# credential rather than being read as "no date".
_DATE_FORMATS = (
    "%Y%m%d%H%M%SZ",  # LDAP generalized time
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y",
)


def parse_date(text: str) -> datetime | None:
    """A Unity date as an aware datetime, None for blank, ValueError otherwise.

    A date with no zone is taken as this machine's local time: the service
    runs on site, beside the appliance whose clock wrote it.
    """
    text = (text or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for fmt in _DATE_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"unrecognised Unity date {text!r}") from None
    return parsed if parsed.tzinfo else parsed.astimezone()


def _is_trigger(token: dict[str, Any]) -> bool:
    return token["embossed_number"].strip().lower() == TRIGGER_VALUE


class AvigilonAdapter:
    def __init__(self, host: str, username: str, password: str, verify_ssl: bool = False):
        self._client = UnityClient(host=host, username=username, password=password, verify_ssl=verify_ssl)
        from . import DESCRIPTOR
        self._descriptor = DESCRIPTOR
        # Filled by list_people(), one cycle at a time.
        self._tokens: dict[str, list[dict[str, Any]]] = {}
        self._held: set[str] = set()

    def descriptor(self) -> PacsDescriptor:
        return self._descriptor

    def test_connection(self) -> ConnectionResult:
        ok, msg = self._client.test_connection()
        return ConnectionResult(ok=ok, message=msg)

    def list_people(self) -> Iterable[Person]:
        # The roster read raises on any failure — the engine then skips the
        # whole cycle, which is right: a partial roster looks like deletions.
        identities = self._client.list_identities()
        self._tokens, self._held = {}, set()
        for row in identities:
            pid = row["id"]
            active = row["status"] not in ("2", "3", "4")  # unknown counts as active: see below
            if active:
                try:
                    self._tokens[pid] = self._client.list_tokens(pid)
                except UnityError as e:
                    logger.warning("Unity: tokens for %s (%s) unread this cycle: %s", pid, row["full_name"], e)
            if any(_is_trigger(t) for t in self._tokens.get(pid, [])):
                try:
                    row = {**self._client.get_identity(pid), "status": row["status"]}
                except UnityError as e:
                    # Without the detail we would push a blank title and
                    # phone; holding the person's tokens makes every phase
                    # leave them alone until the next cycle reads them.
                    logger.warning("Unity: details for %s unread, holding their tokens: %s", pid, e)
                    self._held.add(pid)
            # An unrecognised identity status is read as active on purpose:
            # inactive empties the person's credentials, which revokes passes.
            yield Person(
                id=pid,
                full_name=row["full_name"],
                first_name=row["first_name"],
                last_name=row["last_name"],
                email=row.get("email", ""),
                phone=row.get("phone") or row.get("work_phone", ""),
                title=row.get("title", ""),
                department=row.get("department", ""),
                active=active,
                raw=row,
            )

    def list_credentials(self, person_id: str) -> Iterable[Credential]:
        pid = str(person_id)
        if pid not in self._tokens:
            raise PacsRecordUnavailable(f"Unity tokens for {pid} were not read this cycle")
        return [self._credential(pid, t) for t in self._tokens[pid]]

    def _credential(self, pid: str, token: dict[str, Any]) -> Credential:
        trigger = _is_trigger(token)
        hold = pid in self._held
        dates: dict[str, datetime | None] = {}
        for name in ("activate_date", "deactivate_date"):
            try:
                dates[name] = parse_date(token[name])
            except ValueError as e:
                # A date we cannot read could be an expiry already passed;
                # treating it as "none" would keep a pass alive forever.
                logger.warning("Unity token %s: %s — holding it", token["id"], e)
                dates[name], hold = None, True
        if trigger and not token["internal_number"]:
            logger.warning("Unity token %s is marked accessgrid but has no Internal Number — holding it", token["id"])
            hold = True
        return Credential(
            id=token["id"],
            person_id=pid,
            card_number=token["internal_number"],
            status=_TOKEN_STATUS.get(token["status"], CredentialStatus.UNKNOWN),
            activate_date=dates["activate_date"],
            deactivate_date=dates["deactivate_date"],
            trigger_active=trigger,
            hold=hold,
            raw=token,
        )

    def update_credential_status(
        self, person_id: str, credential_id: str, status: CredentialStatus
    ) -> bool:
        wanted = _WRITABLE_STATUS.get(status)
        if wanted is None:
            return False
        try:
            # Re-read rather than trust the snapshot: the form echoes every
            # field, so it has to be built from what Unity holds right now.
            token = next((t for t in self._client.list_tokens(person_id) if t["id"] == credential_id), None)
            if token is None:
                logger.warning("Unity token %s is no longer on %s — status not written", credential_id, person_id)
                return False
            if token["status"] != wanted:
                self._client.set_token_status(token, wanted)
        except UnityError as e:
            logger.error("Unity: could not set token %s to %s: %s", credential_id, status.value, e)
            return False
        return True

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
