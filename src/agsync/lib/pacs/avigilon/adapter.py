"""Avigilon Unity → vendor-agnostic adapter.

The trigger is per token: Embossed Number set to `accessgrid` (any case).
The card number is the token's Internal Number — never the Embossed Number,
which by then holds the trigger word.

With a Seos template it runs the other way: AccessGrid allocates the card
number, and the hub creates a token holding it on the identity, one per
device, embossed `accessgrid` too and switched off until the pass is
installed. Those tokens are recorded in `unity_tokens` (written.py), which
is the only thing telling them from enrollments; once written, they drive
the pass.

All of a cycle's Unity reads happen in list_people(). The whole roster is
read, because who is missing from it is who phase 3 deletes. Tokens are read
only for the identities Unity's embossed-number search returns plus everyone
we track — one request a person, so reading the whole roster's tokens cost
thousands a cycle. The JSON identity list has no email, phone or title, so
for anyone holding a trigger token whose row lacks them we fetch their
detail too. The XML list carries them. list_credentials() then answers from
what was read, and says plainly when a person was not read — a failed read
is never passed on as "no tokens".
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime
from typing import Any

from ..base import (
    ConnectionResult,
    Credential,
    CredentialIdentity,
    CredentialStatus,
    PacsDescriptor,
    PacsRecordUnavailable,
    Person,
)
from . import written
from .client import UnityAuthError, UnityClient, UnityError

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
    "%Y%m%d%H%M%S%z",  # LDAP generalized time, as a live box sends it: 20260520164208Z (UTC)
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


def _tracked_people() -> set[str] | None:
    """Everyone we hold a pass for, or None if the table can't be read.

    Rows with a pass only, as the snapshot counts them: a row left pending by
    a failed issue has nothing to lose, and one for somebody since removed
    from Unity would otherwise have every cycle read the roster twice.
    """
    try:
        from ....sync import tracking

        return {t.pacs_person_id for t in tracking.all_tracked()
                if t.ag_card_id and t.status not in ("deleted", "deduped")}
    except Exception as e:  # noqa: BLE001 — the caller falls back to reading everyone
        logger.warning("Unity: could not read the tracking table: %s", e)
        return None


class AvigilonAdapter:
    def __init__(self, host: str, username: str, password: str, verify_ssl: bool = False,
                 mode: str = "desfire", pair: bool = False):
        self._client = UnityClient(host=host, username=username, password=password, verify_ssl=verify_ssl)
        # From the AccessGrid template (descriptor.derives_mode_from_template).
        self.seos = mode == "seos"
        from . import DESCRIPTOR
        self._descriptor = DESCRIPTOR
        # Filled by list_people(), one cycle at a time.
        self._tokens: dict[str, list[dict[str, Any]]] = {}
        self._held: set[str] = set()
        self._inactive: set[str] = set()

    # A deactivated identity's passes are suspended, not deleted: the snapshot
    # asks list_credentials for the inactive people it holds passes for.
    suspends_inactive_people = True

    def descriptor(self) -> PacsDescriptor:
        return self._descriptor

    def test_connection(self) -> ConnectionResult:
        ok, msg = self._client.test_connection()
        return ConnectionResult(ok=ok, message=msg)

    def list_people(self) -> Iterable[Person]:
        # The roster read raises on any failure — the engine then skips the
        # whole cycle, which is right: a partial roster looks like deletions.
        identities = self._client.list_identities()
        tracked = _tracked_people()
        missing = (tracked or set()) - {r["id"] for r in identities}
        if missing:
            # A paged read can skip a row when Unity gains someone mid-read,
            # and a skipped cardholder reads to phase 3 as deleted. Anyone
            # still absent from a second read is gone; anyone in either stays.
            logger.warning("Unity: %d tracked identities missing from the roster — reading it again", len(missing))
            again = self._client.list_identities()
            identities = list(({r["id"]: r for r in identities} | {r["id"]: r for r in again}).values())
        to_read = self._whose_tokens(tracked)
        self._tokens, self._held, self._inactive = {}, set(), set()
        for row in identities:
            pid = row["id"]
            active = row["status"] not in ("2", "3", "4")  # unknown counts as active: see below
            if not active:
                self._inactive.add(pid)
            if active and (to_read is None or pid in to_read):
                try:
                    self._tokens[pid] = self._client.list_tokens(pid)
                except UnityError as e:
                    logger.warning("Unity: tokens for %s (%s) unread this cycle: %s", pid, row["full_name"], e)
            if not row["email"] and any(_is_trigger(t) for t in self._tokens.get(pid, [])):
                try:
                    detail = self._client.get_identity(pid)
                    # Fill gaps only: a live box's detail lacked fields its list had.
                    row = {**row, **{k: v for k, v in detail.items() if v and not row.get(k)}}
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

    def _whose_tokens(self, tracked: set[str] | None) -> set[str] | None:
        """Whose tokens to read this cycle: None means everyone's.

        The search only adds people. Everyone tracked is read whatever it
        says — it skips inactive tokens, and a token whose trigger was
        removed is exactly what phase 2 needs to see — so a search that
        misses someone delays an enrollment and never revokes a pass.
        """
        if tracked is None:
            return None
        try:
            hits = self._client.search_embossed(TRIGGER_VALUE)
        except UnityAuthError:
            raise
        except UnityError as e:
            logger.warning("Unity: embossed-number search failed, reading every identity's tokens: %s", e)
            return None
        return {r["id"] for r in hits} | tracked

    def list_credentials(self, person_id: str) -> Iterable[Credential]:
        pid = str(person_id)
        if pid in self._inactive:
            # Read only now: the snapshot asks just for the few inactive
            # people holding a pass, not the thousands that don't. An
            # inactive identity's tokens open no door, whatever they say.
            try:
                tokens = self._client.list_tokens(pid)
            except UnityError as e:
                raise PacsRecordUnavailable(f"Unity tokens for inactive {pid} unread: {e}") from e
            return [replace(c, status=CredentialStatus.SUSPENDED) for c in self._credentials(pid, tokens)]
        if pid not in self._tokens:
            raise PacsRecordUnavailable(f"Unity tokens for {pid} were not read this cycle")
        return self._credentials(pid, self._tokens[pid])

    def _credentials(self, pid: str, tokens: list[dict[str, Any]]) -> list[Credential]:
        if not self.seos:
            return [self._credential(pid, t) for t in tokens]
        # One credential per enrollment token. Tokens we created are not
        # credentials of their own: they are the pass's, under its enrollment.
        ours = written.for_person(pid)
        mine = {e["token_id"] for e in ours}
        by_id = {t["id"]: t for t in tokens}
        return [
            self._seos_credential(pid, t, [e for e in ours if e["credential_id"] == t["id"]], by_id)
            for t in tokens if _is_trigger(t) and t["id"] not in mine
        ]

    def _seos_credential(self, pid: str, enrollment: dict[str, Any], ours: list[dict],
                         tokens: dict[str, dict[str, Any]]) -> Credential:
        """A Seos pass, judged by the tokens we wrote for it once there are any.

        Every one has to be there and on. AccessGrid suspends a pass, not one
        device, so a token deleted or switched off in Unity suspends it — but
        a token off because we wrote it off, to wait for the install, is our
        own hold. Before anything is written, the enrollment's status stands.
        """
        dates, hold = self._dates(enrollment)
        if not ours:
            status = _TOKEN_STATUS.get(enrollment["status"], CredentialStatus.UNKNOWN)
        else:
            status, waiting = CredentialStatus.ACTIVE, False
            for e in ours:
                token = tokens.get(e["token_id"])
                if token is None or (token["status"] != "1" and e["active"]):
                    logger.info("Unity token %s of %s is %s — suspending the pass", e["token_id"], pid,
                                "gone" if token is None else "switched off in Unity")
                    status = CredentialStatus.SUSPENDED
                    break
                waiting = waiting or token["status"] != "1"
            if status is CredentialStatus.ACTIVE and waiting:
                status = CredentialStatus.AWAITING_INSTALL
        return Credential(
            id=enrollment["id"],
            person_id=pid,
            # Empty on purpose: AccessGrid allocates the card number.
            card_number="",
            status=status,
            activate_date=dates["activate_date"],
            deactivate_date=dates["deactivate_date"],
            trigger_active=True,
            allocate_identity=True,
            hold=hold or pid in self._held,
            raw={"enrollment": enrollment, "written": [e["token_id"] for e in ours]},
        )

    @staticmethod
    def _dates(token: dict[str, Any]) -> tuple[dict[str, datetime | None], bool]:
        hold = False
        dates: dict[str, datetime | None] = {}
        for name in ("activate_date", "deactivate_date"):
            try:
                dates[name] = parse_date(token[name])
            except ValueError as e:
                # A date we cannot read could be an expiry already passed;
                # treating it as "none" would keep a pass alive forever.
                logger.warning("Unity token %s: %s — holding it", token["id"], e)
                dates[name], hold = None, True
        return dates, hold

    def _credential(self, pid: str, token: dict[str, Any]) -> Credential:
        trigger = _is_trigger(token)
        dates, hold = self._dates(token)
        hold = hold or pid in self._held
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
        if self.seos:
            return self._set_written_status(str(person_id), credential_id, status)
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

    # -- Seos: tokens the hub writes ---------------------------------------

    @property
    def supports_credential_writeback(self) -> bool:
        return self.seos

    @property
    def supports_credential_retirement(self) -> bool:
        """Only tokens we created are ours to delete."""
        return self.seos

    def _set_written_status(self, pid: str, credential_id: str, status: CredentialStatus) -> bool:
        """Switch the pass's tokens on (installed) or off (suspended, or not
        installed yet). A token we switched on that is now off was switched
        off by an operator, and is not switched back on."""
        ours = written.for_credential(pid, credential_id)
        if not ours:
            return False
        want = status is CredentialStatus.ACTIVE
        code = "1" if want else "2"
        try:
            tokens = {t["id"]: t for t in self._client.list_tokens(pid)}
            if want and any(e["active"] and e["token_id"] in tokens and tokens[e["token_id"]]["status"] != "1"
                            for e in ours):
                logger.warning("Unity: a token of %s %s was switched off in Unity after we switched it on "
                               "— leaving it off", pid, credential_id)
                return False
            for e in ours:
                token = tokens.get(e["token_id"])
                if token is None:
                    logger.warning("Unity token %s of %s is gone — not setting its status", e["token_id"], pid)
                    continue
                if token["status"] != code:
                    self._client.set_token_status(token, code)
                written.set_active(e["token_id"], want)
        except UnityError as e:
            logger.error("Unity: could not set the tokens of %s %s to %s: %s", pid, credential_id, status.value, e)
            return False
        return True

    def write_back_credentials(self, person_id: str, credential_id: str,
                               identities: list[CredentialIdentity]) -> bool:
        """Create a token for each AccessGrid credential not yet in Unity.

        Created switched off; the install gate switches it on. Offered again
        every cycle, so a credential already written is skipped, and a token
        holding its number that we created but failed to record is adopted
        rather than created twice. A number recorded on another pass is
        never written: two people would hold the same card.
        """
        pid = str(person_id)
        recorded = written.all_tokens()
        mine = {(e["facility_code"], e["card_number"]) for e in recorded
                if (e["person_id"], e["credential_id"]) == (pid, credential_id)}
        elsewhere = {(e["facility_code"], e["card_number"]): e["person_id"] for e in recorded
                     if (e["person_id"], e["credential_id"]) != (pid, credential_id)}
        new = [i for i in identities if (str(i.site_code), str(i.card_number)) not in mine]
        if not new:
            return True
        known = {e["token_id"] for e in recorded}
        tokens = self._client.list_tokens(pid)
        for identity in new:
            card = (str(identity.site_code), str(identity.card_number))
            if card in elsewhere:
                logger.error("Unity: AccessGrid reports card %s/%s on the pass of %s (%s), but it is "
                             "already written for %s — not writing", *card, pid, credential_id, elsewhere[card])
                return False
            adopt = next((t for t in tokens if t["internal_number"] == card[1] and _is_trigger(t)
                          and t["id"] not in known and t["id"] != credential_id), None)
            token_id = adopt["id"] if adopt else self._client.create_token(pid, card[1], TRIGGER_VALUE, "2")
            written.add(pid, credential_id, token_id, *card)
        return True

    def retire_credentials(self, person_id: str, credential_id: str,
                           identities: list[CredentialIdentity]) -> int:
        """Delete the tokens of credentials AccessGrid deleted or unlinked."""
        pid = str(person_id)
        gone = {(str(i.site_code), str(i.card_number)) for i in identities}
        retired = 0
        for e in written.for_credential(pid, credential_id):
            if (e["facility_code"], e["card_number"]) not in gone:
                continue
            try:
                self._client.delete_token(pid, e["token_id"])
            except UnityError as err:
                if any(t["id"] == e["token_id"] for t in self._client.list_tokens(pid)):
                    logger.error("Unity: could not delete token %s of %s: %s", e["token_id"], pid, err)
                    continue
            written.remove(e["token_id"])
            retired += 1
        return retired

    def written_credentials(self) -> dict[tuple[str, str], list[str]]:
        """Card numbers written into Unity, per pass, from our own record."""
        out: dict[tuple[str, str], list[str]] = {}
        for e in written.all_tokens():
            out.setdefault((e["person_id"], e["credential_id"]), []).append(e["card_number"])
        return out


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
