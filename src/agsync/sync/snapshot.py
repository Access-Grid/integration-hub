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
    ag_cards_by_token: dict[tuple[str, str], Any] = field(default_factory=dict)
    ag_card_by_id: dict[str, Any] = field(default_factory=dict)

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
        emp = getattr(card, "employee_id", None)
        if emp:
            snap.ag_cards_by_employee.setdefault(emp, []).append(card)
            metadata = getattr(card, "metadata", None) or {}
            token_id = metadata.get("pacs_credential_id") or metadata.get("avigilon_token_id")
            if token_id:
                snap.ag_cards_by_token[(emp, token_id)] = card

    logger.info(
        "Snapshot complete: %d people, %d credentials, %d AG cards (%d token-matched)",
        len(snap.people), snap.total_credentials, len(snap.ag_card_by_id),
        len(snap.ag_cards_by_token),
    )
    return snap
