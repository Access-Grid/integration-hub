"""Phase 6 — Sync person field changes (name/phone/title/department) to AG.

Compare current PACS values to the last_synced_* values stored in the
tracking row. If anything changed, push an AG update and store the new
values.

Email is pushed only for a pass whose card the PACS supplies (DESFire,
SmartTap), and in an update of its own. On a Seos pass AccessGrid rejects
it — "Unexpected parameters provided" — and when it rode with the other
fields that failed the whole update, so a rename never reached it either.
There a changed address is reported once rather than retried forever.
"""

from __future__ import annotations

import logging

from ... import dryrun, notifications
from ...ag import AccessGrid, AccessGridError
from .. import tracking
from ..snapshot import Snapshot

logger = logging.getLogger(__name__)


def run(snapshot: Snapshot, ag: AccessGrid) -> int:
    updated = 0
    logger.info("Phase 6: Checking for field changes")

    for tracked in tracking.all_tracked():
        if tracked.status in ("deleted", "deduped") or not tracked.ag_card_id:
            continue
        if getattr(tracked, "last_known_ag_state", "") == "deleted":
            continue  # deleted in AccessGrid: nothing to update until a new pass is issued
        person = snapshot.people.get(tracked.pacs_person_id)
        if person is None:
            continue
        creds = snapshot.credentials_by_person.get(tracked.pacs_person_id, [])
        cred = next((c for c in creds if c.id == tracked.pacs_credential_id), None)
        if cred is not None and cred.hold:
            continue
        identity_changes = _credential_changes(snapshot, tracked, person, cred) if cred is not None else {}
        expires = getattr(cred, "deactivate_date", None)
        expiration = expires.isoformat() if expires else ""

        changes = dict(identity_changes)
        if person.full_name and person.full_name != tracked.last_synced_full_name:
            changes["full_name"] = person.full_name
        # A phone emptied in the PACS is emptied on the pass, as title is.
        if person.phone != tracked.last_synced_phone:
            changes["phone_number"] = person.phone
        if person.title != tracked.last_synced_title:
            changes["title"] = person.title
        # A pass issued before departments were sent (None) gets the
        # person's, if they have one; after that, any change, a cleared one
        # included, as with title.
        if tracked.last_synced_department is None:
            if person.department:
                changes["department"] = person.department
        elif person.department != tracked.last_synced_department:
            changes["department"] = person.department
        # The PACS's expiry. Recorded the first time it is seen, rather than
        # pushed, so passes issued before this was tracked are not all
        # rewritten at once; a date the PACS no longer states is not cleared.
        if (
            expiration
            and tracked.last_synced_expiration is not None
            and expiration != tracked.last_synced_expiration
        ):
            changes["expiration_date"] = expiration

        email_changed = person.email != tracked.last_synced_email
        first_expiry = tracked.last_synced_expiration is None and bool(expiration)
        # Recorded, so a pass issued before departments were sent is judged
        # against "" from now on rather than looked at again every cycle.
        first_department = tracked.last_synced_department is None
        if not changes and not email_changed and not first_expiry and not first_department:
            continue

        # Only where the PACS supplies the card: AccessGrid rejects an
        # address on a Seos pass, whose card it allocated.
        push_email = email_changed and cred is not None and not cred.allocate_identity
        if email_changed and not push_email:
            problem = (
                f"{person.full_name} ({tracked.pacs_person_id}): address is now {person.email or '(none)'} "
                f"but the pass was issued to {tracked.last_synced_email or '(none)'}. "
                "The Seos email is unchanged; review whether the pass needs reissuing."
            )
            logger.warning("  %s", problem)
            notifications.notify_card_problem(
                f"email-change:{tracked.pacs_person_id}:{tracked.pacs_credential_id}", problem,
            )

        if changes:
            try:
                logger.info(
                    "  Updating AG card %s — %s",
                    tracked.ag_card_id, ", ".join(changes.keys()),
                )
                if dryrun.active():
                    dryrun.would(
                        "update AG card %s — %s",
                        tracked.ag_card_id, ", ".join(changes.keys()),
                    )
                    continue
                # Credential edits must refresh the wallet payload as well.
                ag.access_cards.update(
                    card_id=tracked.ag_card_id,
                    update_type="metadata+payload" if identity_changes else "metadata", **changes,
                )
                updated += 1
                if identity_changes:
                    metadata = identity_changes["metadata"]
                    snapshot.ag_cards_by_site_card[(metadata["site_code"], metadata["card_number"])] = snapshot.resolve_ag_card(
                        tracked.ag_card_id, tracked.pacs_person_id, tracked.pacs_credential_id, tracked.sync_ref,
                    )
            except AccessGridError as e:
                logger.error("  Field update failed for %s: %s", tracked.ag_card_id, e)
                # Leave the tracking row alone so the change is retried, and
                # so a failed name push does not get recorded as done.
                continue

        email = person.email
        if push_email:
            if dryrun.active():
                dryrun.would("update AG card %s — email", tracked.ag_card_id)
                continue
            if _push_email(ag, tracked, person.email):
                updated += 1
            else:
                email = tracked.last_synced_email  # the rest is recorded; the address is retried

        # Recorded even when the only change was an address we cannot push:
        # noticing it once is useful, noticing it every cycle forever is not.
        tracking.update_field_tracking(
            tracked.pacs_person_id, tracked.pacs_credential_id,
            full_name=person.full_name,
            email=email,
            phone=person.phone,
            title=person.title,
            expiration=expiration or (tracked.last_synced_expiration or ""),
            department=person.department,
        )

    logger.info("Phase 6 done: %d field update(s)", updated)
    return updated


def _credential_changes(snapshot, tracked, person, cred) -> dict:
    """Non-Seos credentials may change; preserve metadata used for duplicate checks."""
    if cred.allocate_identity or not cred.trigger_active or not cred.card_number:
        return {}
    cards = snapshot.resolve_ag_cards(
        tracked.ag_card_id, tracked.pacs_person_id, tracked.pacs_credential_id, tracked.sync_ref,
    )
    metadata = next(((getattr(c, "metadata", None) or {}) for c in cards
                     if (getattr(c, "metadata", None) or {}).get("card_number")), None)
    if metadata is None:
        return {}
    site = str(cred.site_code or metadata.get("site_code") or "")
    number = str(cred.card_number)
    if (str(metadata.get("site_code") or ""), str(metadata["card_number"])) == (site, number):
        return {}
    if cred.raw.get("encoding_unavailable"):
        problem = f"Unity: pass {tracked.ag_card_id} needs card {site}/{number}, but its format cannot be encoded. Credential unchanged."
        logger.error("%s", problem)
        notifications.notify_card_problem(f"card-format:{person.id}:{cred.id}", problem)
        return {}
    from .phase1_provision import refuse_duplicate

    if refuse_duplicate(snapshot, person.id, person.full_name, cred, site):
        return {}
    logger.info("  Updating credential for pass %s: %s/%s → %s/%s", tracked.ag_card_id,
                metadata.get("site_code", ""), metadata["card_number"], site, number)
    changes = {"metadata": {**metadata, "site_code": site, "card_number": number}}
    if cred.file_data and cred.send_file_data:
        changes["file_data"] = cred.file_data
    else:
        changes["card_number"] = number
        if site:
            changes["site_code"] = int(site)
    return changes


def _push_email(ag: AccessGrid, tracked, email: str) -> bool:
    """Send a changed address on its own, so a rejection cannot hold back a
    rename sent beside it."""
    logger.info("  Updating AG card %s — email", tracked.ag_card_id)
    try:
        ag.access_cards.update(card_id=tracked.ag_card_id, update_type="metadata", email=email)
    except AccessGridError as e:
        logger.error("  Email update rejected for %s (%s): %s — retrying next cycle",
                     tracked.ag_card_id, tracking.label(tracked), e)
        return False
    return True
