"""Temporary diagnostics for a suspend that does not take.

Unticking a card in Millennium leaves the pass active and the box ticked
back on within a cycle. The suspected cause is that phase 2 suspends the
unified pass id recorded on the tracking row, while phase 4's install gate
judges the same credential by re-reading the pair's halves — so if
suspending the pass does not leave the halves suspended, the gate runs
second and reverts the operator.

These lines run inside a live cycle, so the only thing worth testing about
them is that they cannot break it.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from agsync.lib.pacs.base import CredentialStatus
from agsync.sync.phases import phase2_local_to_ag as p2
from agsync.sync.phases import phase4_ag_to_local as p4

TRACKED = SimpleNamespace(
    pacs_person_id="11666", pacs_credential_id="seos",
    ag_card_id="mFhrBKqYwbOGtZ8", sync_ref="5ca68dbaabce8bf5",
    last_known_ag_state="active",
)
CRED = SimpleNamespace(status=CredentialStatus.SUSPENDED, allocate_identity=True)


class _Exploding:
    """Everything about this object raises."""

    def __getattr__(self, name):
        raise RuntimeError(f"no {name}")


def test_both_prefixes_match():
    assert p2.SUSPEND_DEBUG == p4.SUSPEND_DEBUG == "[SUSPEND-DEBUG]"


def test_the_before_line_names_whether_the_id_is_a_card(caplog):
    apple = SimpleNamespace(id="H081IcyvXEtpPM4", state="active", devices=[])
    snapshot = SimpleNamespace(resolve_ag_cards=lambda *a, **k: [apple])

    with caplog.at_level(logging.INFO):
        p2._suspend_debug_before(snapshot, TRACKED, "active", CRED)

    assert "[SUSPEND-DEBUG]" in caplog.text
    assert "NOT one of them" in caplog.text, "the pass id is not a card id"


def test_the_before_line_says_so_when_the_id_is_a_card(caplog):
    card = SimpleNamespace(id=TRACKED.ag_card_id, state="active", devices=[])
    snapshot = SimpleNamespace(resolve_ag_cards=lambda *a, **k: [card])

    with caplog.at_level(logging.INFO):
        p2._suspend_debug_before(snapshot, TRACKED, "active", CRED)

    assert "is one of them" in caplog.text


def test_the_gate_line_says_when_it_is_about_to_write(caplog):
    apple = SimpleNamespace(id="H081IcyvXEtpPM4", state="active", devices=[])

    with caplog.at_level(logging.INFO):
        p4._gate_debug(TRACKED, [apple], CredentialStatus.ACTIVE, CRED)

    assert "WRITING active to the PACS card" in caplog.text


def test_the_gate_line_says_when_it_will_not(caplog):
    apple = SimpleNamespace(id="H081IcyvXEtpPM4", state="suspended", devices=[])

    with caplog.at_level(logging.INFO):
        p4._gate_debug(TRACKED, [apple], CredentialStatus.SUSPENDED, CRED)

    assert "leaving it alone" in caplog.text


# ---------------------------------------------------------------------
# They run in a live cycle, so they must not be able to stop one
# ---------------------------------------------------------------------


@pytest.mark.parametrize("call", [
    lambda: p2._suspend_debug_before(_Exploding(), TRACKED, "active", CRED),
    lambda: p2._suspend_debug_before(
        SimpleNamespace(resolve_ag_cards=lambda *a, **k: [_Exploding()]),
        TRACKED, "active", CRED),
    lambda: p2._suspend_debug_after(_Exploding(), TRACKED, _Exploding()),
    lambda: p2._suspend_debug_after(
        SimpleNamespace(resolve_ag_cards=lambda *a, **k: [_Exploding()]),
        TRACKED, _Exploding()),
    lambda: p4._gate_debug(TRACKED, [_Exploding()], CredentialStatus.ACTIVE, CRED),
    lambda: p4._gate_debug(_Exploding(), [], CredentialStatus.ACTIVE, CRED),
])
def test_a_diagnostic_cannot_break_the_cycle(call, caplog):
    with caplog.at_level(logging.INFO):
        call()  # must not raise


def test_a_failed_re_read_is_reported_rather_than_thrown(caplog):
    """The re-read is the point of the exercise, so a failure has to be
    visible rather than swallowed into a generic line."""
    card = SimpleNamespace(id="H081IcyvXEtpPM4", state="active", devices=[])

    def boom(_cid):
        raise RuntimeError("gone")

    snapshot = SimpleNamespace(resolve_ag_cards=lambda *a, **k: [card])
    ag = SimpleNamespace(access_cards=SimpleNamespace(get=boom))

    with caplog.at_level(logging.INFO):
        p2._suspend_debug_after(snapshot, TRACKED, ag)

    assert "re-read failed: gone" in caplog.text
