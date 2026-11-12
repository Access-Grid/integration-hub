"""Build a single snapshot of both systems at the start of each cycle.

The phases read from this snapshot rather than querying live, so a slow
PACS or a flaky AG response affects only the snapshot stage and produces
a single coherent view of the world for the entire cycle.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..ag import AccessGrid, list_cards
from ..lib.pacs import Credential, PacsAdapter, PacsRecordUnavailable, Person

logger = logging.getLogger(__name__)


@dataclass
class Snapshot:
    people: dict[str, Person] = field(default_factory=dict)
    credentials_by_person: dict[str, list[Credential]] = field(default_factory=dict)
    ag_cards_by_employee: dict[str, list[Any]] = field(default_factory=dict)
    # (employee_id, pacs_credential_id) -> every card carrying that pair.
    # A list because one issue can produce more than one card: a card
    # template pair yields one per platform, and keeping a single card here
    # meant a pass resolved to half of itself.
    ag_cards_by_token: dict[tuple[str, str], list[Any]] = field(default_factory=dict)
    ag_card_by_id: dict[str, Any] = field(default_factory=dict)
    # Our own reference, stamped into card metadata at issue time. The exact
    # join: every card one issue produced shares it, so a card template pair
    # resolves to both of its halves. A list, because that is the point.
    ag_cards_by_sync_ref: dict[str, list[Any]] = field(default_factory=dict)
    # Indexed by (site_code, card_number) — both stored as strings so callers
    # don't have to worry about int-vs-str coercion at lookup time. Populated
    # from card metadata so dedupe sees only cards we (or sister instances)
    # have tagged with this convention.
    ag_cards_by_site_card: dict[tuple[str, str], Any] = field(default_factory=dict)
    # Cards re-read individually this cycle, keyed by id. See detailed_cards.
    ag_card_detail: dict[str, Any] = field(default_factory=dict)
    # Of those, the ids whose re-read failed, so the listed card stands in.
    # A listed card answers `devices: []`, so for these "no device" means
    # "unknown", never "not installed".
    ag_card_detail_failed: set[str] = field(default_factory=set)
    # (person, credential) whose pass phase 2 tried to suspend or resume
    # this cycle. Even a rejected write must not reactivate the PACS card.
    # The listing above predates that call, so
    # phase 4 reading it would take the old state for an AccessGrid change
    # and write it back over the PACS change that caused it.
    pushed_to_ag: set[tuple[str, str]] = field(default_factory=set)
    # False when AccessGrid's pass listing could not be read: the indexes
    # above are then empty, not "no passes", and nothing may be issued on
    # the strength of them.
    ag_cards_read: bool = True
    # (person, credential) phase 1 tried to issue this cycle. A failed call
    # may still have created the pass, and the listing above predates it,
    # so phase 5 leaves these for a cycle that can see it.
    issue_attempted: set[tuple[str, str]] = field(default_factory=set)
    # True for a PACS whose expired card suspends its pass (phase 2) rather
    # than deletes it (phase 3): the adapter's suspends_expired_cards.
    suspends_expired: bool = False

    def resolve_ag_cards(
        self,
        ag_card_id: str | None,
        person_id: str = "",
        credential_id: str = "",
        sync_ref: str = "",
    ) -> list[Any]:
        """Every card belonging to one tracked issue, best index first.

        The sync reference is preferred because we chose it: it is stamped
        into the metadata of every card an issue produced, so it survives the
        one case ids do not — a card template pair is tracked by a unified id
        that never appears in the template's own listing, while both of its
        halves carry the reference.

        The id comes next, then employee id + pacs_credential_id for cards
        issued before references existed. That last one is a weak key: it
        repeats if a credential was ever issued twice, so it is a fallback
        rather than the plan.
        """
        if sync_ref:
            cards = self.ag_cards_by_sync_ref.get(sync_ref)
            if cards:
                return list(cards)
        if ag_card_id:
            card = self.ag_card_by_id.get(ag_card_id)
            if card is not None:
                return [card]
        if sync_ref:
            # Issued with a reference, but not in this cycle's listing: a
            # pass issued during this cycle. The token would find every
            # earlier issue of the credential instead, and phases 3 and 4
            # would act on their deleted and suspended cards.
            return []
        if person_id and credential_id:
            cards = self.ag_cards_by_token.get((person_id, credential_id))
            if cards:
                return list(cards)
        return []

    def detailed_cards(
        self,
        ag: Any,
        ag_card_id: str | None,
        person_id: str = "",
        credential_id: str = "",
        sync_ref: str = "",
    ) -> list[Any]:
        """Every card of one issue, each re-read for its device credentials.

        The listing answers `devices: []` — and a device is where a second
        installation's own card number lives, an Apple Watch beside its
        phone. A card taken from the listing alone therefore reports only
        the phone's number, so the watch's never reaches the PACS and is
        never released from it.

        Cached per cycle by card id: phases 3 and 4 both ask about the same
        cards, and a listed card is kept as the answer if the read fails —
        stale beats absent, and absent is what would look like a deletion.
        """
        out: list[Any] = []
        for card in self.resolve_ag_cards(
            ag_card_id, person_id, credential_id, sync_ref
        ):
            card_id = getattr(card, "id", None)
            if not card_id:
                out.append(card)
                continue
            if card_id not in self.ag_card_detail:
                try:
                    self.ag_card_detail[card_id] = ag.access_cards.get(card_id)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "Could not re-read AG card %s — its listing stands in, "
                        "which does not say what it is installed on: %s", card_id, e,
                    )
                    self.ag_card_detail[card_id] = card
                    self.ag_card_detail_failed.add(card_id)
            out.append(self.ag_card_detail[card_id])
        return out

    def resolve_ag_card(
        self,
        ag_card_id: str | None,
        person_id: str = "",
        credential_id: str = "",
        sync_ref: str = "",
    ) -> Any | None:
        """One card for a tracked issue, for callers that need a single state.

        A live one if there is one, then one not deleted. AccessGrid deletes
        the half of a pair nobody installed and keeps listing it; read first,
        its "deleted" hid the live half's state, and suspends stopped syncing
        both ways. The same goes for a pair whose installed half was then
        unlinked: the unlink is the pass's state, not the abandoned half's
        delete.
        """
        from .phases.writeback import is_dead, state_of

        cards = self.resolve_ag_cards(ag_card_id, person_id, credential_id, sync_ref)
        return next(
            (c for c in cards if not is_dead(c)),
            next((c for c in cards if state_of(c) != "deleted"), cards[0] if cards else None),
        )

    @property
    def total_credentials(self) -> int:
        return sum(len(v) for v in self.credentials_by_person.values())

    @property
    def trigger_credentials(self) -> int:
        return sum(
            sum(1 for c in creds if c.trigger_active)
            for creds in self.credentials_by_person.values()
        )


def build_snapshot(
    pacs: PacsAdapter,
    ag: AccessGrid,
    template_id: str,
) -> Snapshot:
    snap = Snapshot()
    snap.suspends_expired = bool(getattr(pacs, "suspends_expired_cards", False))

    logger.info("Building snapshot — fetching people from PACS")
    for person in pacs.list_people():
        snap.people[person.id] = person

    logger.info("PACS: %d people loaded", len(snap.people))

    # Cardholders we are tracking. An unread stranger is routine; an unread
    # cardholder we hold a pass for is the thing that hides a bug, because
    # every phase downstream reads their absence as "nothing to do".
    tracking_read = True
    try:
        from . import tracking

        tracked_people = {
            t.pacs_person_id for t in tracking.all_tracked()
            if t.ag_card_id and t.status not in ("deleted", "deduped")
        }
    except Exception as e:  # noqa: BLE001 — diagnostics must not break a cycle
        logger.debug("Could not read the tracking table for diagnostics: %s", e)
        tracked_people = set()
        tracking_read = False
    # An adapter whose inactive people keep their credentials (suspended,
    # not gone) is read for the inactive people we hold a pass for; reading
    # them as no credentials would have phase 3 delete those passes.
    suspends_inactive = getattr(pacs, "suspends_inactive_people", False)

    unread = 0
    for pid, person in snap.people.items():
        if not person.active:
            if not suspends_inactive:
                snap.credentials_by_person[pid] = []
                continue
            if not tracking_read:
                continue  # unread: no entry, so no phase acts on them
            if pid not in tracked_people:
                snap.credentials_by_person[pid] = []
                continue
        try:
            creds = list(pacs.list_credentials(pid))
        except PacsRecordUnavailable:
            # Routine. An adapter that can tell which records matter skips
            # the rest, so this is most of the roster on most cycles — one
            # line each would bury everything else in the log. The handful
            # we are tracking are named, because for those it is not routine.
            unread += 1
            if pid in tracked_people:
                logger.warning(
                    "PACS: %s (%s) holds a pass we issued but was not read this "
                    "cycle — phases 2, 3 and 4 will all skip them",
                    pid, person.full_name,
                )
            continue  # no entry at all — see phase 3
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to fetch credentials for %s (%s): %s", pid, person.full_name, e)
            continue
        snap.credentials_by_person[pid] = creds

    logger.info(
        "PACS: %d credentials total, %d trigger-active",
        snap.total_credentials, snap.trigger_credentials,
    )
    missing = sorted(tracked_people - set(snap.credentials_by_person))
    if missing:
        logger.warning(
            "PACS: %d tracked cardholder(s) produced no credential this cycle: %s",
            len(missing), ", ".join(missing),
        )
    if unread:
        logger.info("PACS: %d record(s) not read this cycle", unread)

    logger.info("Fetching AG cards for template %s", template_id)
    try:
        cards = list_cards(ag, template_id)
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to list AG cards — no pass is issued this cycle: %s", e)
        cards = []
        snap.ag_cards_read = False

    from .phases.writeback import is_dead

    for card in cards:
        snap.ag_card_by_id[card.id] = card
        metadata = getattr(card, "metadata", None) or {}
        ref = metadata.get("sync_ref")
        if ref:
            snap.ag_cards_by_sync_ref.setdefault(str(ref), []).append(card)
        emp = getattr(card, "employee_id", None)
        if emp:
            snap.ag_cards_by_employee.setdefault(emp, []).append(card)
            token_id = metadata.get("pacs_credential_id") or metadata.get("avigilon_token_id")
            if token_id:
                snap.ag_cards_by_token.setdefault((emp, token_id), []).append(card)

        site = metadata.get("site_code")
        card_no = metadata.get("card_number")
        if card_no is not None:
            # A card with no site code (H10302) under "". A live card is
            # never displaced by a dead one with the same number: AccessGrid
            # keeps listing deleted passes, and a re-issued card's deleted
            # pass must not hide its live one, or the live one hide nothing.
            key = (str(site) if site is not None else "", str(card_no))
            current = snap.ag_cards_by_site_card.get(key)
            if current is None or is_dead(current) or not is_dead(card):
                snap.ag_cards_by_site_card[key] = card

    logger.info(
        "Snapshot complete: %d people, %d credentials, %d AG cards "
        "(%d ref-matched, %d token-matched, %d site+card-matched)",
        len(snap.people), snap.total_credentials, len(snap.ag_card_by_id),
        len(snap.ag_cards_by_sync_ref), len(snap.ag_cards_by_token),
        len(snap.ag_cards_by_site_card),
    )
    return snap
