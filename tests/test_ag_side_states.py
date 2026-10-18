"""A pass going unlinked or deleted is news, not an instruction.

Both are device-side or AccessGrid-side events, and neither says anything
about whether the cardholder's card in the PACS should work. The card
belongs to the operator: switching it off because somebody changed phones
would revoke access nobody revoked.

So these states push no card status — and that was already true, but it
happened without a word, which made "the holder dropped their pass" and
"nothing happened" look identical in the log. Each is now reported once, on
the cycle it changes.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agsync.lib.pacs.base import CredentialStatus
from agsync.sync.phases import phase4_ag_to_local
from agsync.sync.snapshot import Snapshot

PID, CRED = "11587", "seos"


def _tracked(last_known: str = "installed"):
    return SimpleNamespace(
        pacs_person_id=PID, pacs_credential_id=CRED, ag_card_id="pass-1",
        status="active", sync_ref="ref-1", last_known_ag_state=last_known,
    )


@pytest.fixture
def cycle(monkeypatch):
    """Phase 4 over one tracked credential whose pass is in a given state."""
    def _run(ag_state: str, pacs_status=CredentialStatus.ACTIVE):
        row = _tracked()
        monkeypatch.setattr(
            phase4_ag_to_local.tracking, "all_tracked", lambda: [row]
        )
        pushed: list[str] = []
        monkeypatch.setattr(
            phase4_ag_to_local.tracking, "update_last_known_ag_state",
            lambda pid, cred, state: pushed.append(state),
        )
        card = SimpleNamespace(
            id="pass-1", state=ag_state, details=None, site_code="66",
            card_number="5001", expiration_date=None, devices=[],
        )
        snap = Snapshot()
        snap.ag_cards_by_sync_ref["ref-1"] = [card]
        snap.credentials_by_person[PID] = [
            SimpleNamespace(
                id=CRED, status=pacs_status, allocate_identity=True,
                trigger_active=True, card_number="5001", site_code="66",
            )
        ]
        written: list[tuple] = []
        pacs = SimpleNamespace(
            supports_status_writeback=True,
            update_credential_status=lambda *a: written.append(a) or True,
        )
        count = phase4_ag_to_local.run(snap, pacs, None)
        return SimpleNamespace(count=count, written=written, recorded=pushed)

    return _run


# =====================================================================
# No card status is pushed
# =====================================================================


@pytest.mark.parametrize("state", ["unlinked", "deleted"])
def test_no_card_status_is_pushed(cycle, state):
    """The card belongs to the operator, and neither state is about it."""
    result = cycle(state)

    assert result.written == []


@pytest.mark.parametrize("state", ["unlinked", "deleted"])
def test_the_state_is_still_recorded_so_it_is_only_said_once(cycle, state):
    result = cycle(state)

    assert state in result.recorded


# =====================================================================
# But it is said
# =====================================================================


def test_an_unlinked_pass_is_reported(cycle, caplog):
    with caplog.at_level("WARNING"):
        cycle("unlinked")

    assert "is now unlinked" in caplog.text
    assert "removed it from their device" in caplog.text
    assert "card in the PACS is untouched" in caplog.text


def test_a_deleted_pass_is_reported(cycle, caplog):
    with caplog.at_level("WARNING"):
        cycle("deleted")

    assert "is now deleted" in caplog.text
    assert "card in the PACS is untouched" in caplog.text


def test_the_cardholder_and_the_pass_are_both_named(cycle, caplog):
    """So somebody can act on it without going looking."""
    with caplog.at_level("WARNING"):
        cycle("unlinked")

    assert PID in caplog.text
    assert "pass-1" in caplog.text


def test_an_ordinary_state_is_not_reported_as_news(cycle, caplog):
    with caplog.at_level("WARNING"):
        cycle("suspended")

    assert "card in the PACS is untouched" not in caplog.text
