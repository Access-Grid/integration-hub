"""Phase 5 — Retry failed provisions.

Walks the tracking table for rows with sync_error set and retry_count
below MAX_RETRIES. Re-runs the same provision logic as phase 1.

Tracking rows that exceed MAX_RETRIES are left alone with their error
message visible to the operator on the status page.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from ...ag import AccessGrid, AccessGridError
from .. import tracking
from ..snapshot import Snapshot

logger = logging.getLogger(__name__)

MAX_RETRIES = 3


def run(
    snapshot: Snapshot,
    ag: AccessGrid,
    template_id: str,
    site_code: str = "",
    dedupe_by_site_card: bool = False,
    extra_metadata: dict | None = None,
) -> int:
    failed = tracking.failed_records(MAX_RETRIES)
    if not failed:
        logger.debug("Phase 5: No failed records to retry")
        return 0

    logger.info("Phase 5: Retrying %d failed provision(s)", len(failed))
    succeeded = 0

    for tracked in failed:
        person = snapshot.people.get(tracked.pacs_person_id)
        creds = snapshot.credentials_by_person.get(tracked.pacs_person_id, [])
        cred = next((c for c in creds if c.id == tracked.pacs_credential_id), None)

        if person is None or cred is None or not cred.trigger_active:
            # Underlying record is gone or no longer eligible — clear retry.
            tracking.remove(tracked.pacs_person_id, tracked.pacs_credential_id)
            continue

        if not person.email and not person.phone:
            tracking.record_error(
                tracked.pacs_person_id, tracked.pacs_credential_id,
                "still missing email and phone",
            )
            continue

        if (
            dedupe_by_site_card
            and site_code
            and cred.card_number
        ):
            hit = snapshot.ag_cards_by_site_card.get(
                (str(site_code), str(cred.card_number))
            )
            if hit is not None:
                existing_id = getattr(hit, "id", None)
                logger.info(
                    "  Dedupe: AG card %s already exists for site=%s card=%s — marking %s deduped",
                    existing_id, site_code, cred.card_number, person.full_name,
                )
                tracking.mark_deduped(
                    pacs_person_id=tracked.pacs_person_id,
                    pacs_credential_id=tracked.pacs_credential_id,
                    existing_ag_card_id=existing_id,
                    full_name=person.full_name,
                )
                continue

        now = datetime.now(UTC)
        metadata: dict = dict(extra_metadata or {})
        metadata["pacs_credential_id"] = cred.id
        if site_code:
            metadata["site_code"] = site_code
        if cred.card_number:
            metadata["card_number"] = str(cred.card_number)

        params: dict = {
            "card_template_id": template_id,
            "employee_id": tracked.pacs_person_id,
            "full_name": person.full_name,
            "start_date": (cred.activate_date or now).isoformat(),
            "expiration_date": (cred.deactivate_date or (now + timedelta(days=365))).isoformat(),
            "metadata": metadata,
        }
        if site_code and site_code.isdigit():
            params["site_code"] = int(site_code)
        if person.email:
            params["email"] = person.email
        if person.phone:
            params["phone_number"] = person.phone
        if person.title:
            params["title"] = person.title
        if cred.card_number:
            params["card_number"] = cred.card_number

        try:
            logger.info(
                "  Retry attempt for %s/%s (attempt %d/%d)",
                tracked.pacs_person_id, tracked.pacs_credential_id,
                tracked.retry_count + 1, MAX_RETRIES,
            )
            card = ag.access_cards.provision(**params)
            ag_card_id = getattr(card, "id", None)
            ag_state = (getattr(card, "state", "active") or "active").lower()
            tracking.upsert(
                pacs_person_id=tracked.pacs_person_id,
                pacs_credential_id=tracked.pacs_credential_id,
                ag_card_id=ag_card_id,
                full_name=person.full_name,
                employee_id=tracked.pacs_person_id,
                status="active" if ag_state in ("active", "created") else ag_state,
                last_synced_email=person.email,
                last_synced_phone=person.phone,
                last_synced_full_name=person.full_name,
                last_synced_title=person.title,
                last_known_ag_state=ag_state,
            )
            succeeded += 1
            logger.info("  Retry succeeded for %s", person.full_name)
        except AccessGridError as e:
            logger.warning("  Retry failed for %s: %s", person.full_name, e)
            tracking.record_error(tracked.pacs_person_id, tracked.pacs_credential_id, str(e))
        except Exception as e:  # noqa: BLE001
            logger.warning("  Retry crashed for %s: %s", person.full_name, e)
            tracking.record_error(
                tracked.pacs_person_id, tracked.pacs_credential_id,
                f"{type(e).__name__}: {e}",
            )

    logger.info("Phase 5 done: %d/%d succeeded", succeeded, len(failed))
    return succeeded
