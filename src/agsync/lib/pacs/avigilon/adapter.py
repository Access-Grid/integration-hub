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

from .... import dryrun
from ..base import (
    ConnectionResult,
    Credential,
    CredentialIdentity,
    CredentialStatus,
    PacsDescriptor,
    PacsRecordUnavailable,
    Person,
)
from ..emails import first_valid_email, synthesize_email
from . import wiegand, written
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
                 card_format_label: str = "", facility_code: str = "", card_format_bits: str = "",
                 card_format_layout: dict[str, Any] | None = None, email_domain: str = "",
                 notify_email: str = ""):
        self._client = UnityClient(host=host, username=username, password=password, verify_ssl=verify_ssl)
        # From the AccessGrid template (descriptor.derives_mode_from_template).
        self.seos = mode == "seos"
        # The card format chosen in setup (card_format_params): a token names
        # none, so its facility code is every DESFire card's site code, and
        # its layout how a card is sent — 26-bit as site code + card number,
        # longer as file_data.
        self.card_format = card_format
        # Seos passes need an email. A holder with none gets
        # first.last.<identity id>@ this domain; with no domain, their
        # enrollment is held and the operator mailed. (notify_email is read
        # by notifications from the stored params, not here.)
        self.email_domain = (email_domain or "").strip().lstrip("@")
        self.card_format_label = card_format_label or card_format
        self.facility_code = str(facility_code or "")
        self._layout: wiegand.Layout | None = None
        if card_format_layout:
            try:
                self._layout = wiegand.Layout.from_row(card_format_layout)
            except wiegand.UnencodableCard as e:
                logger.warning("Unity card format %s: stored layout unreadable, reading it again: %s",
                               self.card_format_label, e)
        if card_format and not self.seos:
            logger.info("Unity card format %s: %s", self.card_format_label,
                        _layout_note(self._layout) if self._layout else "layout read on the first cycle")
        from . import DESCRIPTOR
        self._descriptor = DESCRIPTOR
        # Filled by list_people(), one cycle at a time.
        self._tokens: dict[str, list[dict[str, Any]]] = {}
        self._held: set[str] = set()
        self._inactive: set[str] = set()
        self._issued: set[tuple[str, str]] = set()  # (person, credential) holding a pass
        self._no_email: dict[str, str] = {}  # Seos: id -> name of each person read with no usable email

    # A deactivated identity's passes are suspended, not deleted: the snapshot
    # asks list_credentials for the inactive people it holds passes for.
    suspends_inactive_people = True
    # A token past its end date suspends its pass rather than deleting it:
    # moving the date on resumes the same pass, with no new invite.
    suspends_expired_cards = True
    # A card its holder unlinked is switched off, not deleted (phase 4): kept,
    # it is switched back on if the pass is. Deleting its token left the pass
    # with no card, and the next cycle deleted the pass.
    retires_unlinked_cards = False
    keeps_deleted_pass_tokens = True

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
        layout = wiegand.Layout.from_row(chosen)  # UnencodableCard is a ValueError
        # A format without facility code bits (H10302) needs none.
        if layout.fc_len and not chosen["facility_code"].isdigit():
            raise ValueError(f"card format {_format_label(chosen)} has no facility code")
        logger.info("Unity card format set to %s: %s", _format_label(chosen), _layout_note(layout))
        return {**params, "card_format": format_id, "card_format_label": _format_label(chosen),
                "facility_code": chosen["facility_code"] if layout.fc_len else "",
                "card_format_bits": chosen["total_bits"], "card_format_layout": layout.as_params()}

    # Phase 3 judges a short read by the tracked people it holds, not by the
    # size of a roster this adapter never reads.
    reads_whole_roster = False

    def list_people(self) -> Iterable[Person]:
        passes = _tracked_passes()
        tracked = {t.pacs_person_id: t for t in passes}
        rows, unread = self._identities(tracked)
        if self.card_format and not self.seos and self._layout is None:
            self._read_layout()
        self._tokens, self._held, self._inactive, self._no_email = {}, set(), set(), {}
        self._issued = {(t.pacs_person_id, t.pacs_credential_id) for t in passes}
        for pid in unread:
            # Its last synced fields, so nothing reads as changed, and no
            # tokens, so list_credentials says it was not read.
            t = tracked[pid]
            yield Person(id=pid, full_name=t.last_synced_full_name, email=t.last_synced_email,
                         phone=t.last_synced_phone, title=t.last_synced_title,
                         department=t.last_synced_department or "")
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
            # Spec, revoke: missing from the list but still holding a tracked
            # accessgrid token — kept, and reported. Tracked means the token a
            # pass was issued for or, Seos, one the hub wrote for such a pass.
            # Only an Active one: the search lists no switched-off token, so a
            # suspended holder is always missing from it.
            if detailed and self._search_ok and any(
                _is_trigger(t) and t["status"] == "1" and t["id"] in self._tracked_token_ids(pid)
                for t in self._tokens.get(pid, [])
            ):
                _problem(f"roster-mismatch:{pid}",
                         f"Unity: identity {pid} ({row['full_name']}) was absent from the enrollment search, "
                         "but its detail still holds a tracked active accessgrid token. Pass retained; review the search.")
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
            email = row.get("email", "")
            if self.seos and pid not in self._held:
                email = self._seos_email(pid, row)
            elif email and not first_valid_email(email):
                # AccessGrid rejects the issue outright over an address like
                # this, even for a holder it could reach by phone. A holder
                # who already has a pass keeps the address it was last sent:
                # an invalid value is no change, not an emptied email.
                kept = tracked[pid].last_synced_email if pid in tracked else ""
                logger.warning("Unity: identity %s (%s) has %r as their email, which is not an address "
                               "— %s", pid, row["full_name"], email,
                               f"keeping {kept} on their pass" if kept else "leaving it off their pass")
                email = kept
            # An unrecognised identity status is read as active on purpose:
            # inactive empties the person's credentials, which revokes passes.
            yield Person(
                id=pid,
                full_name=row["full_name"],
                first_name=row["first_name"],
                last_name=row["last_name"],
                email=email,
                phone=row.get("phone") or row.get("work_phone", ""),
                title=row.get("title", ""),
                department=row.get("department", ""),
                active=active,
                raw=row,
            )

    def _tracked_token_ids(self, pid: str) -> set[str]:
        """Ids of this identity's tokens that back a pass: its enrollments, and the hub's own tokens."""
        issued = {cid for p, cid in self._issued if p == pid}
        return issued | {e["token_id"] for e in written.for_person(pid)
                         if e["token_id"] and e["credential_id"] in issued}

    def _read_layout(self) -> None:
        """The chosen format's layout, for a setup made before it was stored.

        Read once; a failed read leaves it unknown, so this cycle's
        enrollments wait rather than go out in a format we have not checked.
        """
        try:
            row = next((f for f in self._client.list_card_formats() if f["id"] == self.card_format), None)
            if row is None:
                raise UnityError(f"card format {self.card_format} is no longer defined in Unity")
            self._layout = wiegand.Layout.from_row(row)
        except (UnityError, wiegand.UnencodableCard) as e:
            logger.warning("Unity card format %s: layout unread, enrollments wait for it: %s",
                           self.card_format_label, e)
            return
        logger.info("Unity card format %s: %s", self.card_format_label, _layout_note(self._layout))

    def _encoding(self, pid: str, token: dict[str, Any]) -> tuple[str, bool]:
        """(file_data, hold) for an enrollment not yet issued, in the chosen format.

        26-bit (H10301) goes as site code + card number, so no file_data,
        but its numbers must still fit. A card that cannot be sent is held
        and mailed: issuing it in the wrong format makes a pass no reader
        takes.
        """
        if not self.card_format:
            return "", False  # no format chosen: the site code from settings stands, as before
        if self._layout is None:
            logger.warning("Unity token %s of %s: the card format's layout is unread — not issuing until it is",
                           token["id"], pid)
            return "", True
        number = token["internal_number"]
        try:
            file_data = wiegand.encode(self._layout, self.facility_code, number)
        except wiegand.UnencodableCard as e:
            _card_problem(pid, (self.facility_code, number),
                          f"Unity: token {token['id']} of identity {pid} is marked accessgrid, but its card "
                          f"{self.facility_code}/{number} cannot be sent in card format {self.card_format_label}: "
                          f"{e} — not issuing a pass")
            return "", True
        if self._layout.total_bits == 26:
            return "", False
        logger.info("Unity token %s of %s: card %s/%s goes as %s file_data", token["id"], pid,
                    self.facility_code, number, wiegand.standard(self._layout))
        return file_data, False

    def _seos_email(self, pid: str, row: dict[str, str]) -> str:
        """The address a Seos pass is issued to: Unity's, if it is one.

        Otherwise a synthetic one, if setup gave a domain for them, or none,
        which holds the person's enrollments (_missing_email).
        """
        email = first_valid_email(row.get("email", ""))
        if email:
            return email
        if row.get("email"):
            logger.warning("Unity: identity %s (%s) has %r as their email, which is not an address",
                           pid, row["full_name"], row["email"])
        if self.email_domain:
            made = synthesize_email(row["first_name"], row["last_name"], pid, self.email_domain)
            if made:
                logger.info("Unity: identity %s (%s) has no email — using %s for their Seos pass",
                            pid, row["full_name"], made)
                return made
        self._no_email[pid] = row["full_name"]
        return ""

    def _missing_email(self, pid: str, cid: str) -> bool:
        """An enrollment with no pass yet, for someone with no address: held, and mailed."""
        if (pid, cid) in self._issued or pid not in self._no_email:
            return False
        _problem(f"{pid}:{cid}:email",
                 f"Unity: identity {pid} ({self._no_email[pid]}) has token {cid} marked accessgrid, but no "
                 f"email address, and AccessGrid needs one for a Seos pass — not issuing it. Add an email to "
                 f"the identity in Unity, or set a domain for synthetic addresses in Settings.")
        return True

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
        self._search_ok = True
        try:
            hits = self._client.search_embossed(TRIGGER_VALUE)
        except UnityAuthError:
            raise
        except UnityError as e:
            logger.warning("Unity: embossed-number search failed — reading tracked people only, "
                           "new enrollments wait for the next cycle: %s", e)
            self._search_ok = False
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

    def forget_credentials(self, person_id: str, credential_id: str) -> None:
        """Forget Unity writeback rows only after AccessGrid revocation succeeded."""
        for entry in written.for_credential(person_id, credential_id):
            written.remove(entry["row"])

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
        # held too: after a crash their ownership requires operator review.
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

        One active device keeps the pass active. Independent device blocks
        remain local to that token; only a pass with no active device is
        suspended. Our own install/suspension holds are distinguished from
        operator changes in the persisted token record. Once the enrollment
        and every token are gone, phase 3 deletes the pass.
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
        for entry in ours:
            token = tokens.get(entry["token_id"])
            if token and token["internal_number"] != entry["card_number"]:
                _problem(f"seos-number:{pid}:{cid}",
                         f"Unity: identity {pid}, pass {cid}: Seos token {entry['token_id']} now has "
                         f"card {token['internal_number']}, issued as {entry['facility_code']}/"
                         f"{entry['card_number']}. AccessGrid credentials are unchanged; review the edit.")
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
            hold = hold or self._not_yet_issuable(pid, cid, status) or self._missing_email(pid, cid)
        else:
            ours = [written.observe_status(e, tokens[e["token_id"]]["status"])
                    if e["token_id"] in tokens and not gone(e) else e for e in ours]
            live = [tokens[e["token_id"]] for e in ours if e["token_id"] in tokens and not gone(e)]
            hold = hold or any(t["status"] not in _TOKEN_STATUS for t in live)
            if any(t["status"] == "1" for t in live):
                status = CredentialStatus.ACTIVE
            elif any(gone(e) or e["operator_disabled"] for e in ours):
                status = CredentialStatus.SUSPENDED
            else:
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
        file_data = ""
        encoding_available = not self.card_format or (self._layout is not None and wiegand.standard(self._layout))
        if trigger and not hold and ((pid, token["id"]) not in self._issued or encoding_available):
            file_data, hold = self._encoding(pid, token)
        return Credential(
            id=token["id"],
            person_id=pid,
            card_number=token["internal_number"],
            # Empty when no format was chosen: the site code from settings stands.
            site_code=self.facility_code,
            file_data=file_data,
            send_file_data=bool(file_data),
            status=status,
            activate_date=dates["activate_date"],
            deactivate_date=dates["deactivate_date"],
            trigger_active=trigger,
            hold=hold,
            raw={**token, "encoding_unavailable": not encoding_available},
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
                if dryrun.active():
                    dryrun.would("switch Unity token %s of %s %s", credential_id, person_id,
                                 "on" if wanted == "1" else "off")
                    return False
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

    def credential_status_matches(self, person_id: str, credential_id: str,
                                  status: CredentialStatus) -> bool:
        """An aggregate status cannot prove every backing device matches.

        Use this cycle's observations and the persisted device policy. A
        newly written token or interrupted write must be reconciled even
        if another device already makes the aggregate look correct.
        """
        if not self.seos:
            return True  # non-Seos keeps the engine's aggregate comparison
        ours = written.for_credential(str(person_id), credential_id)
        tokens = {t["id"]: t for t in self._tokens.get(str(person_id), [])}
        want = status is CredentialStatus.ACTIVE
        return bool(ours) and all(
            e["token_id"] in tokens and e["pending_active"] is None
            and (tokens[e["token_id"]]["status"] == "1") == (want and not e["operator_disabled"])
            for e in ours
        )

    def _set_written_status(self, pid: str, credential_id: str, status: CredentialStatus) -> bool:
        """Apply a whole-pass hold without erasing independent device blocks."""
        ours = [e for e in written.for_credential(pid, credential_id) if e["token_id"]]
        if not ours:
            return False
        want = status is CredentialStatus.ACTIVE
        complete = True
        try:
            tokens = {t["id"]: t for t in self._client.list_tokens(pid)}
            for e in ours:
                token = tokens.get(e["token_id"])
                if token is None:
                    _problem(f"{pid}:{e['token_id']}",
                             f"Unity: AccessGrid wants the pass of identity {pid} ({credential_id}) {status.value}, "
                             f"but its token {e['token_id']} is no longer in Unity — status not written to it")
                    complete = False
                    continue
                if token["status"] not in _TOKEN_STATUS:
                    logger.warning("Unity token %s: status unknown — leaving it unchanged", e["token_id"])
                    complete = False
                    continue
                e = written.observe_status(e, token["status"])
                target = want and not e["operator_disabled"]
                # Keep a device's Expired/Not Yet Active status intact.
                if e["operator_disabled"] and token["status"] != "1":
                    continue
                code = "1" if target else "2"
                if dryrun.active():
                    if token["status"] != code:
                        dryrun.would("switch Unity token %s of %s %s", e["token_id"], pid, "on" if target else "off")
                    continue
                if token["status"] != code:
                    written.begin_status_write(e["token_id"], target)
                    self._client.set_token_status(token, code)
                written.set_active(e["token_id"], target)
        except UnityError as e:
            self._status_unwritable(pid, credential_id, status, e)
            return False
        return complete and not dryrun.active()

    def write_back_credentials(self, person_id: str, credential_id: str,
                               identities: list[CredentialIdentity]) -> bool:
        """Create a token for each AccessGrid credential not yet in Unity,
        then delete the enrollment token.

        In that order, each step only once the one before has held: the row
        is recorded, then the token created (switched off; the install gate
        switches it on) and its id recorded, and only when every credential
        has its token does the enrollment go. Offered again every cycle, so a
        credential already written is skipped, and an enrollment whose
        delete failed is deleted then. A matching untracked token is held
        for review: its number and marker cannot prove who created it. A
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
            if dryrun.active():
                dryrun.would("write Unity token for card %s/%s on %s (%s), switched off", *card, pid, credential_id)
                continue
            if not fresh:
                tokens, fresh = self._client.list_tokens(pid), True
            untracked = next((t for t in tokens if t["internal_number"] == card[1] and _is_trigger(t)
                              and t["id"] not in known and t["id"] != credential_id), None)
            if untracked:
                self._ambiguous_token(pid, credential_id, card[1], untracked["id"])
                return False
            # Checked before anything is recorded: a refused card leaves no row.
            if not self._number_is_free(pid, credential_id, card):
                return False
            if card in mine:
                row = mine[card]["row"]
            else:
                enrollment = next((t for t in tokens if t["id"] == credential_id), None)
                dates = self._dates(enrollment)[0] if enrollment else {}
                row = written.add(pid, credential_id, *card,
                                  *(d.isoformat() if (d := dates.get(n)) else None
                                    for n in ("activate_date", "deactivate_date")))
            token_id = self._client.create_token(pid, card[1], TRIGGER_VALUE, "2")
            written.set_token(row, token_id)
            known.add(token_id)
        if dryrun.active():
            # Nothing written. With every token already there, the enrollment's delete is what's left.
            if tokens is not None and all(card in mine and mine[card]["token_id"] for card in
                                          ((str(i.site_code), str(i.card_number)) for i in identities)) \
                    and any(t["id"] == credential_id for t in tokens):
                dryrun.would("delete enrollment token %s of %s — its pass has its own tokens", credential_id, pid)
            return False
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
        """Delete the tokens of credentials AccessGrid deleted (not unlinked: retires_unlinked_cards)."""
        pid = str(person_id)
        gone = {(str(i.site_code), str(i.card_number)) for i in identities}
        retired = 0
        for e in written.for_credential(pid, credential_id):
            if (e["facility_code"], e["card_number"]) not in gone:
                continue
            if dryrun.active():
                dryrun.would("delete Unity token %s of %s — AccessGrid no longer backs card %s/%s",
                             e["token_id"] or "(id unrecorded)", pid, e["facility_code"], e["card_number"])
                continue
            token_id = e["token_id"]
            if not token_id:
                # Its token may have been created with the id never recorded.
                # Keep ambiguous reservations without adopting or deleting a token.
                if not self._unrecorded_token_absent(pid, credential_id, e["card_number"]):
                    continue
            if token_id:
                try:
                    self._client.delete_token(pid, token_id)
                except UnityError as err:
                    if not self._token_gone(pid, token_id):
                        logger.error("Unity: could not delete token %s of %s, keeping its record to retry "
                                     "next cycle: %s", token_id, pid, err)
                        continue
                logger.info("Unity: deleted token %s of %s — AccessGrid no longer backs card %s/%s",
                            token_id, pid, e["facility_code"], e["card_number"])
            written.remove(e["row"])
            retired += 1
        return retired

    def release_deleted_pass(self, person_id: str, credential_id: str) -> bool:
        """A pass AccessGrid deleted whole, its card since switched off: is it back on?

        Phase 4 asks this every cycle until it is. True means the card was
        switched back on in Unity and is now ready to be issued a new pass
        like any enrollment; the caller then forgets the deleted pass.

        DESFire: the enrollment token itself, which stays where it is. Seos:
        any token we wrote for the pass. The first one switched on becomes the
        enrollment for the new pass — forgotten here, so it reads as one, and
        deleted once the new pass has its own tokens. Every other token we
        wrote for it carries a number of the deleted pass and is deleted now.
        Nothing is forgotten while a delete fails; the next cycle asks again.
        """
        pid = str(person_id)
        tokens = self._tokens.get(pid)
        if tokens is None:
            return False  # not read this cycle
        by_id = {t["id"]: t for t in tokens}

        def on(token_id: str | None) -> bool:
            token = by_id.get(token_id or "")
            return token is not None and _is_trigger(token) and token["status"] == "1"

        if not self.seos:
            return on(credential_id)
        ours = written.for_credential(pid, credential_id)
        enrollment = next((e for e in ours if on(e["token_id"])), None)
        if enrollment is None:
            return False
        # Resolve uncertainty before deleting any known tokens or releasing
        # their records. An untracked match must never become ours by inference.
        if not dryrun.active() and any(
            not self._unrecorded_token_absent(pid, credential_id, e["card_number"])
            for e in ours if not e["token_id"]
        ):
            return False
        if dryrun.active():
            dryrun.would("make Unity token %s of %s the enrollment for a new pass, deleting the other "
                         "tokens of deleted pass %s", enrollment["token_id"], pid, credential_id)
            return False
        for e in ours:
            if e is enrollment:
                continue
            token_id = e["token_id"]
            if token_id and token_id in by_id:
                try:
                    self._client.delete_token(pid, token_id)
                except UnityError as err:
                    if not self._token_gone(pid, token_id):
                        logger.error("Unity: could not delete token %s of %s, left from deleted pass %s — "
                                     "retrying next cycle: %s", token_id, pid, credential_id, err)
                        return False
                logger.info("Unity: deleted token %s of %s — its card %s/%s was on a pass AccessGrid deleted",
                            token_id, pid, e["facility_code"], e["card_number"])
                self._tokens[pid] = [t for t in self._tokens[pid] if t["id"] != token_id]
            written.remove(e["row"])
        written.remove(enrollment["row"])
        logger.info("Unity: token %s of %s was switched back on after AccessGrid deleted its pass — it is now "
                    "the enrollment for a new pass", enrollment["token_id"], pid)
        return True

    def _unrecorded_token_absent(self, pid: str, credential_id: str, number: str) -> bool:
        """May a pending reservation be forgotten without touching any token?

        A matching number is not ownership evidence. Keep ambiguous rows so
        their tokens cannot turn into new enrollments, and ask for review.
        An unread list keeps the reservation too; a confirmed missing identity
        or a complete read without a candidate allows just the row to go.
        """
        try:
            tokens = self._client.list_tokens(pid)
        except UnityError as err:
            try:
                self._client.get_identity(pid)
            except UnityNotFound:
                return True
            except UnityError:
                pass
            logger.warning("Unity: could not read the tokens of %s to find the unrecorded token for card %s, "
                           "keeping its record to retry next cycle: %s", pid, number, err)
            return False
        known = {e["token_id"] for e in written.all_tokens() if e["token_id"]}
        token = next((t for t in tokens if t["internal_number"] == number and _is_trigger(t)
                      and t["id"] not in known and t["id"] != credential_id), None)
        if token:
            self._ambiguous_token(pid, credential_id, number, token["id"])
            return False
        return True

    def _ambiguous_token(self, pid: str, credential_id: str, number: str, token_id: str) -> None:
        _problem(f"token-ownership:{pid}:{credential_id}:{token_id}",
                 f"Unity: token {token_id} of identity {pid} matches card number {number} for pass "
                 f"{credential_id}, but its token ID was never tracked. Ownership is unknown: "
                 "not adopting, changing or deleting it. Enrollment and pending records are retained; "
                 "review the token and writeback records before retrying.")

    def _token_gone(self, pid: str, token_id: str) -> bool:
        """Is this token no longer in Unity, on Unity's own word?

        Its identity's token list says so, or, when that list cannot be
        read, Unity answers 404 for the identity itself: deleting an
        identity takes its tokens with it. Anything less certain is "not
        gone" — forgetting a token that is still there would have it read
        as a new enrollment, and issued a second pass.
        """
        try:
            return all(t["id"] != token_id for t in self._client.list_tokens(pid))
        except UnityError as err:
            tokens_err = err
        try:
            self._client.get_identity(pid)
        except UnityNotFound:
            logger.info("Unity: identity %s is no longer in Unity, so neither is its token %s — "
                        "forgetting it", pid, token_id)
            return True
        except UnityError as err:
            logger.warning("Unity: could not tell whether token %s of %s is gone: tokens unread (%s), "
                           "identity unread (%s)", token_id, pid, tokens_err, err)
            return False
        logger.warning("Unity: identity %s is there but its tokens could not be read, so token %s "
                       "may still be there: %s", pid, token_id, tokens_err)
        return False

    def written_credentials(self) -> dict[tuple[str, str], list[str]]:
        """Card numbers written into Unity, per pass, from our own record."""
        out: dict[tuple[str, str], list[str]] = {}
        for e in written.all_tokens():
            if e["token_id"]:
                out.setdefault((e["person_id"], e["credential_id"]), []).append(e["card_number"])
        return out


def _layout_note(layout: wiegand.Layout) -> str:
    name = wiegand.standard(layout)
    if name is None:
        return (f"{layout.total_bits}-bit, not a format this sync can encode "
                f"({', '.join(wiegand.STANDARDS)}) — enrollments will be held")
    return f"{name}, sent as " + ("site code + card number" if layout.total_bits == 26 else "file_data")


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
        "pacs.avigilon.email_domain": (
            "Domain for synthetic emails (optional). A Seos pass needs an email: a holder with none "
            "gets first.last.<identity id>@ this domain. Left blank, their pass is not issued and "
            "the notification email below is told."
        ),
        "pacs.avigilon.notify_email": (
            "Notification email (optional): told about cards the sync will not act on"
        ),
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
        "pacs.avigilon.email_domain": (
            "Dominio para correos sintéticos (opcional). Un pase Seos necesita un correo: un titular "
            "sin él recibe nombre.apellido.<id de identidad>@ este dominio. Si se deja en blanco, su "
            "pase no se emite y se avisa al correo de notificación de abajo."
        ),
        "pacs.avigilon.notify_email": (
            "Correo de notificación (opcional): recibe avisos de las tarjetas sobre las que la "
            "sincronización no actúa"
        ),
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
