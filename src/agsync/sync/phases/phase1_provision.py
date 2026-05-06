"""Phase 1 — Provision new credentials (PACS → AccessGrid).

For every PACS credential whose `trigger_active` is True and that we
have not already provisioned, create a new AccessGrid card. We record
the result in the tracking table.

Skip rules (with reasons logged):
  - person inactive
  - person has no email and no phone (AG needs a delivery channel)
  - person has no full_name
  - tracking row exists with a non-NULL ag_card_id (already provisioned)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from ...ag import AccessGrid, AccessGridError
from .. import tracking
from ..snapshot import Snapshot

logger = logging.getLogger(__name__)


def run(snapshot: Snapshot, ag: AccessGrid, template_id: str) -> int:
    provisioned = 0
    skipped = 0
    logger.info("Phase 1: Checking for new credentials to provision")

    for pid, creds in snapshot.credentials_by_person.items():
        person = snapshot.people.get(pid)
        if person is None or not person.active:
            continue
        for cred in creds:
            if not cred.trigger_active:
                skipped += 1
                continue

            existing = tracking.get(pid, cred.id)
            if existing and existing.ag_card_id:
                # Already provisioned — keep tracking row fresh.
                ag_card = snapshot.ag_cards_by_token.get((pid, cred.id))
                if ag_card is None:
                    ag_card = snapshot.ag_card_by_id.get(existing.ag_card_id)
                ag_state = (getattr(ag_card, "state", "") or "").lower() if ag_card else ""
                if ag_state and ag_state != existing.last_known_ag_state:
                    tracking.update_last_known_ag_state(pid, cred.id, ag_state)
                continue

            if not person.full_name:
                logger.warning("  %s: no name — skipping", pid)
                skipped += 1
                continue
            if not person.email and not person.phone:
                logger.warning("  %s (%s): no email or phone — skipping", pid, person.full_name)
                skipped += 1
                continue

            now = datetime.now(timezone.utc)
            start_date = (cred.activate_date or now).isoformat()
            expiration_date = (cred.deactivate_date or (now + timedelta(days=365))).isoformat()

            params: dict = {
                "card_template_id": template_id,
                "employee_id": pid,
                "full_name": person.full_name,
                "start_date": start_date,
                "expiration_date": expiration_date,
                "metadata": {"pacs_credential_id": cred.id},
            }
            if person.email:
                params["email"] = person.email
            if person.phone:
                params["phone_number"] = person.phone
            if person.title:
                params["title"] = person.title
            if cred.card_number:
                params["card_number"] = cred.card_number

            # Insert tracking row in 'pending' state before the API call so a
            # failed provision still leaves a row for phase 5 to retry.
            tracking.upsert(
                pacs_person_id=pid,
                pacs_credential_id=cred.id,
                ag_card_id=None,
                full_name=person.full_name,
                employee_id=pid,
                status="pending",
                last_synced_email=person.email,
                last_synced_phone=person.phone,
                last_synced_full_name=person.full_name,
                last_synced_title=person.title,
            )

            try:
                logger.info("  Provisioning: %s (%s)", person.full_name, pid)
                card = ag.access_cards.provision(**params)
                ag_card_id = getattr(card, "id", None)
                ag_state = (getattr(card, "state", "active") or "active").lower()
                tracking.upsert(
                    pacs_person_id=pid,
                    pacs_credential_id=cred.id,
                    ag_card_id=ag_card_id,
                    full_name=person.full_name,
                    employee_id=pid,
                    status="active" if ag_state in ("active", "created") else ag_state,
                    last_synced_email=person.email,
                    last_synced_phone=person.phone,
                    last_synced_full_name=person.full_name,
                    last_synced_title=person.title,
                    last_known_ag_state=ag_state,
                )
                provisioned += 1
                logger.info("  Provisioned AG card %s for %s", ag_card_id, person.full_name)
            except AccessGridError as e:
                logger.error("  Provision failed for %s: %s", person.full_name, e)
                tracking.record_error(pid, cred.id, str(e))
            except Exception as e:  # noqa: BLE001
                logger.error("  Unexpected error provisioning %s: %s", person.full_name, e)
                tracking.record_error(pid, cred.id, f"{type(e).__name__}: {e}")

    logger.info("Phase 1 done: %d provisioned, %d skipped", provisioned, skipped)
    return provisioned
