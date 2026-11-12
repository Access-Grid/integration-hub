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

All of a cycle's Unity reads happen in list_people(). The roster is never
read whole — 12,939 identities took 78 s on a live box. Only the identities
Unity's embossed-number search returns are read, plus everyone we track,
each by their own detail. A tracked identity Unity answers 404 for is gone,
which is what phase 3 deletes; one that cannot be read stands in from its
tracking row, unread, so no phase acts on it. Tokens are read for each of
them, and for anyone holding a trigger token whose row lacks an email, the
detail too. list_credentials() then answers from what was read, and says
plainly when a person was not read — a failed read is never passed on as
"no tokens".
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
from .client import UnityAuthError, UnityClient, UnityError, UnityNotFound

logger = logging.getLogger(__name__)

TRIGGER_VALUE = "accessgrid"

# More identities than this for one card number means Unity ignored the
# number and listed everyone: no answer about who holds it.
MAX_NUMBER_HITS = 25

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


def _tracked_passes() -> list[Any]:
    """The tracking rows of every pass we hold.

    Rows with a pass only, as the snapshot counts them: a row left pending by
    a failed issue has nothing to lose. Raises if the table can't be read:
    without it we cannot tell whose passes to check, and a cycle that reads
    only the search would take every pass outside it for deleted.
    """
    from ....sync import tracking

    try:
        rows = tracking.all_tracked()
    except Exception as e:
        raise UnityError(f"the tracking table could not be read: {e}") from e
    return [t for t in rows if t.ag_card_id and t.status not in ("deleted", "deduped")]


class AvigilonAdapter:
    def __init__(self, host: str, username: str, password: str, verify_ssl: bool = False,
                 mode: str = "desfire", pair: bool = False, card_format: str = "",
                 card_format_label: str = "", facility_code: str = "", card_format_bits: str = ""):
        self._client = UnityClient(host=host, username=username, password=password, verify_ssl=verify_ssl)
        # From the AccessGrid template (descriptor.derives_mode_from_template).
        self.seos = mode == "seos"
        # The card format chosen in setup (card_format_params): a token names
        # none, so its facility code is every DESFire card's site code.
        self.facility_code = str(facility_code or "")
        if card_format and not self.seos and str(card_format_bits) != "26":
            logger.warning("Unity card format %s is %s-bit, but cards are sent as 26-bit site code + card "
                           "number — readers expecting %s bits will not read them", card_format_label or card_format,
                           card_format_bits or "an unknown", card_format_bits or "other")
        from . import DESCRIPTOR
        self._descriptor = DESCRIPTOR
        # Filled by list_people(), one cycle at a time.
        self._tokens: dict[str, list[dict[str, Any]]] = {}
        self._held: set[str] = set()
        self._inactive: set[str] = set()
        self._issued: set[tuple[str, str]] = set()  # (person, credential) holding a pass

    # A deactivated identity's passes are suspended, not deleted: the snapshot
    # asks list_credentials for the inactive people it holds passes for.
    suspends_inactive_people = True

    def descriptor(self) -> PacsDescriptor:
        return self._descriptor

    def test_connection(self) -> ConnectionResult:
        ok, msg = self._client.test_connection()
        return ConnectionResult(ok=ok, message=msg)

    def card_formats(self) -> list[tuple[str, str]]:
        """(id, label) for every card format in Unity, for the setup screens."""
        return [(f["id"], _format_label(f)) for f in self._client.list_card_formats()]

    def card_format_params(self, params: dict[str, Any], format_id: str) -> dict[str, Any]:
        """`params` with `format_id` as the card format, read from Unity now.

        Its facility code and length are stored with it, so a cycle needs
        no extra read. Raises ValueError if Unity does not define it.
        """
        format_id = format_id.strip()
        chosen = next((f for f in self._client.list_card_formats() if f["id"] == format_id), None)
        if chosen is None:
            raise ValueError(f"card format {format_id!r} is not defined in Unity")
        if not chosen["facility_code"].isdigit():
            raise ValueError(f"card format {_format_label(chosen)} has no facility code")
        logger.info("Unity card format set to %s", _format_label(chosen))
        return {**params, "card_format": format_id, "card_format_label": _format_label(chosen),
                "facility_code": chosen["facility_code"], "card_format_bits": chosen["total_bits"]}

    # Phase 3 judges a short read by the tracked people it holds, not by the
    # size of a roster this adapter never reads.
    reads_whole_roster = False

    def list_people(self) -> Iterable[Person]:
        passes = _tracked_passes()
        tracked = {t.pacs_person_id: t for t in passes}
        rows, unread = self._identities(tracked)
        self._tokens, self._held, self._inactive = {}, set(), set()
        self._issued = {(t.pacs_person_id, t.pacs_credential_id) for t in passes}
        for pid in unread:
            # Its last synced fields, so nothing reads as changed, and no
            # tokens, so list_credentials says it was not read.
            t = tracked[pid]
            yield Person(id=pid, full_name=t.last_synced_full_name, email=t.last_synced_email,
                         phone=t.last_synced_phone, title=t.last_synced_title)
        for row, detailed in rows.values():
            pid = row["id"]
            active = row["status"] not in ("2", "3", "4")  # unknown counts as active: see below
            if not active:
                self._inactive.add(pid)
            else:
                try:
                    self._tokens[pid] = self._client.list_tokens(pid)
                except UnityError as e:
                    logger.warning("Unity: tokens for %s (%s) unread this cycle: %s", pid, row["full_name"], e)
            if not detailed and not row["email"] and any(_is_trigger(t) for t in self._tokens.get(pid, [])):
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

    def _identities(self, tracked: dict[str, Any]) -> tuple[dict[str, tuple[dict[str, str], bool]], list[str]]:
        """The search's hits and everyone tracked: ({id: (row, from detail)}, unread ids).

        A failed search only delays enrollments: everyone tracked is still
        read, by detail. A tracked identity answering 404 is left out — gone
        — but only once some other detail read has worked this cycle, so a
        box that 404s the detail endpoint itself deletes nobody.
        """
        # Sign in first, so a Unity that is down stops the cycle here rather
        # than reading as a failed search and every tracked person unread.
        self._client.ensure_signed_in()
        try:
            hits = self._client.search_embossed(TRIGGER_VALUE)
        except UnityAuthError:
            raise
        except UnityError as e:
            logger.warning("Unity: embossed-number search failed — reading tracked people only, "
                           "new enrollments wait for the next cycle: %s", e)
            hits = []
        rows = {r["id"]: (r, False) for r in hits}
        unread, not_found = [], []
        for pid in sorted(tracked.keys() - rows.keys()):
            try:
                rows[pid] = (self._client.get_identity(pid), True)
            except UnityNotFound:
                not_found.append(pid)
            except UnityError as e:
                logger.warning("Unity: tracked identity %s unread this cycle: %s", pid, e)
                unread.append(pid)
        if not_found and not self._detail_reads(rows):
            logger.warning("Unity: %d tracked identities answered 404, but no identity's detail read this "
                           "cycle — treating them as unread, not gone", len(not_found))
            return rows, unread + not_found
        for pid in not_found:
            logger.info("Unity: tracked identity %s (%s) is no longer in Unity", pid,
                        tracked[pid].last_synced_full_name)
        return rows, unread

    def _detail_reads(self, rows: dict[str, tuple[dict[str, str], bool]]) -> bool:
        """Has an identity's detail read this cycle? If none was tried, try a hit's."""
        if any(detailed for _, detailed in rows.values()):
            return True
        if not rows:
            return False
        try:
            self._client.get_identity(next(iter(rows)))
        except UnityError:
            return False
        return True

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
        # One credential per pass: an enrollment not yet written, or the
        # tokens we wrote for one, which go on after the enrollment is
        # deleted. A token holding the number of a row still being created is
        # ours too, created before a crash; write_back_credentials adopts it.
        ours = written.for_person(pid)
        mine = {e["token_id"] for e in ours if e["token_id"]}
        creating = {e["card_number"] for e in ours if not e["token_id"]}
        by_id = {t["id"]: t for t in tokens}
        enrollments = [t["id"] for t in tokens if _is_trigger(t) and t["id"] not in mine
                       and t["internal_number"] not in creating]
        passes = list(dict.fromkeys(enrollments + [e["credential_id"] for e in ours]))
        out = []
        for cid in passes:
            enrollment = by_id.get(cid) if cid in enrollments else None
            cred = self._seos_credential(pid, cid, enrollment, [e for e in ours if e["credential_id"] == cid], by_id)
            if cred is not None:
                out.append(cred)
        return out

    def _seos_credential(self, pid: str, cid: str, enrollment: dict[str, Any] | None, ours: list[dict],
                         tokens: dict[str, dict[str, Any]]) -> Credential | None:
        """A Seos pass, judged by the tokens we wrote for it once there are any.

        Every one has to be there and on. AccessGrid suspends a pass, not one
        device, so a token deleted or switched off in Unity suspends it — but
        a token off because we wrote it off, to wait for the install, is our
        own hold. Once the enrollment is deleted and every token we wrote is
        gone too, there is no credential, and phase 3 deletes the pass.
        Before anything is written, the enrollment's status stands.
        """
        def gone(e: dict) -> bool:
            token = tokens.get(e["token_id"]) if e["token_id"] else None
            return bool(e["token_id"]) and (token is None or not _is_trigger(token))

        if enrollment is not None:
            dates, hold = self._dates(enrollment)
        elif all(gone(e) for e in ours):
            logger.info("Unity: every token of %s %s is gone, and the enrollment with them — the pass "
                        "has no credential left", pid, cid)
            return None
        else:
            dates, hold = {n: parse_date(ours[0][n] or "") for n in ("activate_date", "deactivate_date")}, False
        # Dates an operator sets on our tokens win over those kept from the
        # enrollment; they are created undated, so until then the
        # enrollment's stand. Tokens that disagree give the narrowest window:
        # the pass must not outlast the card that ends first.
        own = [self._dates(tokens[e["token_id"]]) for e in ours if e["token_id"] and not gone(e)]
        hold = hold or any(unreadable for _, unreadable in own)
        for name, pick in (("activate_date", max), ("deactivate_date", min)):
            if stated := [d[name] for d, _ in own if d[name]]:
                dates[name] = pick(stated)
        if not ours:
            status = _TOKEN_STATUS.get(enrollment["status"], CredentialStatus.UNKNOWN)
            hold = hold or self._not_yet_issuable(pid, cid, status)
        else:
            status, waiting = CredentialStatus.ACTIVE, False
            for e in ours:
                token = tokens.get(e["token_id"]) if e["token_id"] else None
                if gone(e) or (token is not None and token["status"] != "1" and e["active"]):
                    logger.info("Unity token %s of %s is %s — suspending the pass", e["token_id"], pid,
                                "gone" if gone(e) else "switched off in Unity")
                    status = CredentialStatus.SUSPENDED
                    break
                waiting = waiting or token is None or token["status"] != "1"
            if status is CredentialStatus.ACTIVE and waiting:
                status = CredentialStatus.AWAITING_INSTALL
        return Credential(
            id=cid,
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

    def _not_yet_issuable(self, pid: str, cid: str, status: CredentialStatus) -> bool:
        """An enrollment that is not Active and has no pass yet: held, so no
        pass is issued for it until it is switched on. One with a pass is
        not held — its pass has to follow it to suspended."""
        if status is CredentialStatus.ACTIVE or (pid, cid) in self._issued:
            return False
        logger.info("Unity token %s of %s is marked accessgrid but not Active — not issuing a pass until it is",
                    cid, pid)
        return True

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
        status = _TOKEN_STATUS.get(token["status"], CredentialStatus.UNKNOWN)
        hold = hold or (trigger and self._not_yet_issuable(pid, token["id"], status))
        return Credential(
            id=token["id"],
            person_id=pid,
            card_number=token["internal_number"],
            # Empty when no format was chosen: the site code from settings stands.
            site_code=self.facility_code,
            status=status,
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
                _problem(f"{person_id}:{credential_id}",
                         f"Unity: AccessGrid wants the pass of identity {person_id} {status.value}, but its token "
                         f"{credential_id} is no longer in Unity — status not written")
                return False
            if token["status"] != wanted:
                self._client.set_token_status(token, wanted)
        except UnityError as e:
            self._status_unwritable(str(person_id), credential_id, status, e)
            return False
        return True

    def _status_unwritable(self, pid: str, credential_id: str, status: CredentialStatus, err: UnityError) -> None:
        """A status write that failed: mailed if the identity itself is gone."""
        try:
            self._client.get_identity(pid)
        except UnityNotFound:
            _problem(f"{pid}:{credential_id}",
                     f"Unity: AccessGrid wants the pass of identity {pid} ({credential_id}) {status.value}, "
                     f"but the identity is no longer in Unity — status not written")
            return
        except UnityError:
            pass
        logger.error("Unity: could not set %s %s to %s: %s", pid, credential_id, status.value, err)

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
        ours = [e for e in written.for_credential(pid, credential_id) if e["token_id"]]
        if not ours:
            return False
        want = status is CredentialStatus.ACTIVE
        code = "1" if want else "2"
        try:
            tokens = {t["id"]: t for t in self._client.list_tokens(pid)}
            if want and any(e["active"] and e["token_id"] in tokens and tokens[e["token_id"]]["status"] != "1"
                            for e in ours):
                _problem(f"{pid}:{credential_id}:off",
                         f"Unity: AccessGrid has the pass of identity {pid} ({credential_id}) active, but one of "
                         f"its tokens was switched off in Unity after we switched it on — leaving it off")
                return False
            for e in ours:
                token = tokens.get(e["token_id"])
                if token is None:
                    _problem(f"{pid}:{e['token_id']}",
                             f"Unity: AccessGrid wants the pass of identity {pid} ({credential_id}) {status.value}, "
                             f"but its token {e['token_id']} is no longer in Unity — status not written to it")
                    continue
                if token["status"] != code:
                    self._client.set_token_status(token, code)
                written.set_active(e["token_id"], want)
        except UnityError as e:
            self._status_unwritable(pid, credential_id, status, e)
            return False
        return True

    def write_back_credentials(self, person_id: str, credential_id: str,
                               identities: list[CredentialIdentity]) -> bool:
        """Create a token for each AccessGrid credential not yet in Unity,
        then delete the enrollment token.

        In that order, each step only once the one before has held: the row
        is recorded, then the token created (switched off; the install gate
        switches it on) and its id recorded, and only when every credential
        has its token does the enrollment go. Offered again every cycle, so a
        credential already written is skipped, a token holding its number
        that we created but did not record is adopted rather than created
        twice, and an enrollment whose delete failed is deleted then. A
        number recorded on another pass is never written: two people would
        hold the same card.
        """
        pid = str(person_id)
        recorded = written.all_tokens()
        mine = {(e["facility_code"], e["card_number"]): e for e in recorded
                if (e["person_id"], e["credential_id"]) == (pid, credential_id)}
        elsewhere = {(e["facility_code"], e["card_number"]): e["person_id"] for e in recorded
                     if (e["person_id"], e["credential_id"]) != (pid, credential_id)}
        known = {e["token_id"] for e in recorded if e["token_id"]}
        tokens = self._tokens.get(pid)
        fresh = False
        for identity in identities:
            card = (str(identity.site_code), str(identity.card_number))
            if card in mine and mine[card]["token_id"]:
                continue
            if card in elsewhere:
                logger.error("Unity: AccessGrid reports card %s/%s on the pass of %s (%s), but it is "
                             "already written for %s — not writing", *card, pid, credential_id, elsewhere[card])
                return False
            if not fresh:
                tokens, fresh = self._client.list_tokens(pid), True
            adopt = next((t for t in tokens if t["internal_number"] == card[1] and _is_trigger(t)
                          and t["id"] not in known and t["id"] != credential_id), None)
            # Checked before anything is recorded: a refused card leaves no row.
            if not adopt and not self._number_is_free(pid, credential_id, card):
                return False
            if card in mine:
                row = mine[card]["row"]
            else:
                enrollment = next((t for t in tokens if t["id"] == credential_id), None)
                dates = self._dates(enrollment)[0] if enrollment else {}
                row = written.add(pid, credential_id, *card,
                                  *(d.isoformat() if (d := dates.get(n)) else None
                                    for n in ("activate_date", "deactivate_date")))
            if adopt:
                logger.info("Unity: adopting token %s of %s, created for card %s/%s but not recorded",
                            adopt["id"], pid, *card)
            token_id = adopt["id"] if adopt else self._client.create_token(pid, card[1], TRIGGER_VALUE, "2")
            written.set_token(row, token_id)
            known.add(token_id)
        if tokens is None:
            return True  # not read this cycle: the enrollment's delete waits for a cycle that reads it
        if any(t["id"] == credential_id for t in tokens):
            try:
                self._client.delete_token(pid, credential_id)
            except UnityError as e:
                logger.error("Unity: could not delete enrollment token %s of %s: %s", credential_id, pid, e)
                return False
            logger.info("Unity: deleted enrollment token %s of %s — its pass now has its own tokens",
                        credential_id, pid)
            # This cycle's read too, or the next offer deletes it again.
            self._tokens[pid] = [t for t in tokens if t["id"] != credential_id]
        return True

    def _number_is_free(self, pid: str, credential_id: str, card: tuple[str, str]) -> bool:
        """Is no other identity in Unity holding this card number?

        Asked of Unity's search, then each hit's tokens, since the search is
        loose. A search that fails, or that returns too many to be about this
        number, writes nothing: retried next cycle rather than risk two
        people holding the same card.
        """
        try:
            hits = [r["id"] for r in self._client.search_internal(card[1]) if r["id"] != pid]
            if len(hits) > MAX_NUMBER_HITS:
                raise UnityError(f"{len(hits)} identities returned — the search ignored the number")
            holder = next((h for h in hits
                           if any(t["internal_number"] == card[1] for t in self._client.list_tokens(h))), None)
        except UnityError as e:
            logger.error("Unity: could not check whether card %s/%s is free, not writing it for %s (%s): %s",
                         *card, pid, credential_id, e)
            return False
        if holder is not None:
            _card_problem(pid, card, f"Unity: AccessGrid reports card {card[0]}/{card[1]} on the pass of "
                                     f"{pid} ({credential_id}), but identity {holder} already holds a token "
                                     f"with that number — not writing")
            return False
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
            if e["token_id"]:
                try:
                    self._client.delete_token(pid, e["token_id"])
                except UnityError as err:
                    if any(t["id"] == e["token_id"] for t in self._client.list_tokens(pid)):
                        logger.error("Unity: could not delete token %s of %s: %s", e["token_id"], pid, err)
                        continue
            written.remove(e["row"])
            retired += 1
        return retired

    def written_credentials(self) -> dict[tuple[str, str], list[str]]:
        """Card numbers written into Unity, per pass, from our own record."""
        out: dict[tuple[str, str], list[str]] = {}
        for e in written.all_tokens():
            if e["token_id"]:
                out.setdefault((e["person_id"], e["credential_id"]), []).append(e["card_number"])
        return out


def _format_label(f: dict[str, str]) -> str:
    return f"{f['name'] or f['id']} (FC {f['facility_code'] or '?'}, {f['total_bits'] or '?'}-bit)"


def _card_problem(pid: str, card: tuple[str, str], problem: str) -> None:
    """A card we will not write: logged here, and mailed to the operator."""
    _problem(f"{pid}:{card[0]}/{card[1]}", problem)


def _problem(key: str, problem: str) -> None:
    """Logged here, and mailed to the operator, at most once a while per key."""
    from .... import notifications

    logger.error("%s", problem)
    notifications.notify_card_problem(key, problem)


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
