"""Build a single snapshot of both systems at the start of each cycle.

The phases read from this snapshot rather than querying live, so a slow
PACS or a flaky AG response affects only the snapshot stage and produces
a single coherent view of the world for the entire cycle.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..ag import AccessGrid
from ..lib.pacs import Credential, PacsAdapter, Person

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
        if person_id and credential_id:
            cards = self.ag_cards_by_token.get((person_id, credential_id))
            if cards:
                return list(cards)
        return []

    def resolve_ag_card(
        self,
        ag_card_id: str | None,
        person_id: str = "",
        credential_id: str = "",
        sync_ref: str = "",
    ) -> Any | None:
        """One card for a tracked issue, for callers that need a single state."""
        cards = self.resolve_ag_cards(ag_card_id, person_id, credential_id, sync_ref)
        return cards[0] if cards else None

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

    logger.info("Building snapshot — fetching people from PACS")
    for person in pacs.list_people():
        snap.people[person.id] = person

    logger.info("PACS: %d people loaded", len(snap.people))

    for pid, person in snap.people.items():
        if not person.active:
            snap.credentials_by_person[pid] = []
            continue
        try:
            creds = list(pacs.list_credentials(pid))
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to fetch credentials for %s (%s): %s", pid, person.full_name, e)
            creds = []
        snap.credentials_by_person[pid] = creds

    logger.info(
        "PACS: %d credentials total, %d trigger-active",
        snap.total_credentials, snap.trigger_credentials,
    )

    logger.info("Fetching AG cards for template %s", template_id)
    try:
        cards = ag.access_cards.list(template_id=template_id)
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to list AG cards: %s", e)
        cards = []

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
        if site is not None and card_no is not None:
            snap.ag_cards_by_site_card[(str(site), str(card_no))] = card

    logger.info(
        "Snapshot complete: %d people, %d credentials, %d AG cards "
        "(%d ref-matched, %d token-matched, %d site+card-matched)",
        len(snap.people), snap.total_credentials, len(snap.ag_card_by_id),
        len(snap.ag_cards_by_sync_ref), len(snap.ag_cards_by_token),
        len(snap.ag_cards_by_site_card),
    )
    return snap
