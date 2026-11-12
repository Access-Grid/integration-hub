"""A card number goes on one pass, never two (Avigilon Unity, DESFire).

Unity's descriptor sets unique_card_numbers: a token holding a site code +
card number already on a live pass is refused and the operator mailed,
whatever the dedupe setting says — including a pass issued earlier in the
same cycle. A deleted or unlinked pass holds nobody's working card and does
not count. Other PACS keep the opt-in dedupe, which now also sees passes
issued earlier in the cycle.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agsync.lib.pacs import get_descriptor
from agsync.lib.pacs.base import Credential, CredentialStatus, Person
from agsync.sync import snapshot as snapshot_mod
from agsync.sync import tracking
from agsync.sync.phases import phase1_provision, phase5_retries
from agsync.sync.snapshot import Snapshot, build_snapshot
from tests.test_avigilon_unity import FakeUnity, _adapter, _token


class _Cards:
    def __init__(self):
        self.calls: list[dict] = []

    def provision(self, **params):
        self.calls.append(params)
        return SimpleNamespace(id=f"pass-{len(self.calls)}", state="active", employee_id=params["employee_id"])


@pytest.fixture
def mailed(monkeypatch):
    sent = []
    monkeypatch.setattr("agsync.notifications.notify_card_problem", lambda key, problem: sent.append(problem))
    return sent


def _person(pid):
    return Person(id=pid, full_name=f"Person {pid}", email=f"{pid}@x.test")


def _cred(pid, number="42069", allocate=False):
    return Credential(id=f"t{pid}", person_id=pid, card_number="" if allocate else number, site_code="239",
                      status=CredentialStatus.ACTIVE, trigger_active=True, allocate_identity=allocate)


def _snap(*pids, allocate=False):
    snap = Snapshot()
    for pid in pids:
        snap.people[pid] = _person(pid)
        snap.credentials_by_person[pid] = [_cred(pid, allocate=allocate)]
    return snap


def _listed(state, number="42069", employee="z"):
    return SimpleNamespace(id=f"old-{state}", state=state, employee_id=employee,
                           metadata={"site_code": "239", "card_number": number})


def _run(snap, unique=True, dedupe=False):
    ag = SimpleNamespace(access_cards=_Cards())
    phase1_provision.run(snap, ag, "tpl", require_unique_card=unique, dedupe_by_site_card=dedupe)
    return ag.access_cards.calls


def test_unity_requires_unique_card_numbers_others_do_not():
    assert get_descriptor("avigilon").unique_card_numbers
    assert not get_descriptor("millennium_ultra").unique_card_numbers


def test_two_unity_tokens_with_one_number_get_one_pass_in_one_cycle(monkeypatch, mailed):
    """The real adapter: identities a and b both hold Internal Number 42069."""
    monkeypatch.setattr(snapshot_mod, "list_cards", lambda *a: [])
    fake = FakeUnity([{"cn": p, "plasecName": f"Lee, {p}", "plasecIdstatus": "Active",
                       "plasecidentityEmailaddress": f"{p}@x.test"} for p in ("a", "b")],
                     tokens={"a": [_token("ta")], "b": [_token("tb")]})
    a = _adapter(fake)
    a.card_format, a.facility_code = "", ""
    snap = build_snapshot(a, None, "tpl")
    ag = SimpleNamespace(access_cards=_Cards())
    phase1_provision.run(snap, ag, "tpl", site_code="239", require_unique_card=True)
    assert [c["employee_id"] for c in ag.access_cards.calls] == ["a"]
    assert len(mailed) == 1 and "42069" in mailed[0] and "pass-1" in mailed[0]
    assert tracking.get("b", "tb") is None, "nothing recorded for the refused card: it is retried as it stands"


def test_a_number_already_on_a_live_pass_is_refused(mailed):
    snap = _snap("a")
    snap.ag_cards_by_site_card[("239", "42069")] = _listed("suspended")
    assert _run(snap) == []
    assert "employee z" in mailed[0]


@pytest.mark.parametrize("state", ["deleted", "unlink"])
def test_a_number_on_a_dead_pass_is_issued_again(mailed, state):
    snap = _snap("a")
    snap.ag_cards_by_site_card[("239", "42069")] = _listed(state)
    assert len(_run(snap)) == 1 and mailed == []


def test_a_pass_allocated_by_accessgrid_is_not_checked(mailed):
    assert len(_run(_snap("a", "b", allocate=True))) == 2 and mailed == []


def test_without_the_requirement_both_are_issued_as_before(mailed):
    assert len(_run(_snap("a", "b"), unique=False)) == 2 and mailed == []


def test_the_dedupe_setting_now_sees_a_pass_issued_earlier_in_the_cycle(monkeypatch, mailed):
    deduped = []
    monkeypatch.setattr(phase1_provision.tracking, "mark_deduped", lambda **kw: deduped.append(kw))
    calls = _run(_snap("a", "b"), unique=False, dedupe=True)
    assert len(calls) == 1
    assert deduped[0]["pacs_person_id"] == "b" and deduped[0]["existing_ag_card_id"] == "pass-1"


def test_a_retry_whose_number_another_pass_took_is_refused_and_recorded(monkeypatch, mailed):
    errors = []
    row = SimpleNamespace(pacs_person_id="a", pacs_credential_id="ta", status="error", retry_count=0, ag_card_id=None)
    monkeypatch.setattr(phase5_retries.tracking, "failed_records", lambda limit: [row])
    monkeypatch.setattr(phase5_retries.tracking, "record_error", lambda p, c, e: errors.append(e))
    snap = _snap("a")
    snap.ag_cards_by_site_card[("239", "42069")] = _listed("active")
    ag = SimpleNamespace(access_cards=_Cards())
    phase5_retries.run(snap, ag, "tpl", require_unique_card=True)
    assert ag.access_cards.calls == [] and errors == ["card number already on another pass"] and mailed


@pytest.mark.parametrize("order", [["deleted", "active"], ["active", "deleted"]])
def test_the_index_keeps_the_live_pass_whatever_the_listing_order(monkeypatch, order):
    monkeypatch.setattr(snapshot_mod, "list_cards", lambda *a: [_listed(s) for s in order])

    class NoPeople:
        def list_people(self):
            return []

    snap = build_snapshot(NoPeople(), None, "tpl")
    assert snap.ag_cards_by_site_card[("239", "42069")].state == "active"


def test_a_card_with_no_site_code_is_indexed_too(monkeypatch):
    card = SimpleNamespace(id="x", state="active", employee_id="z", metadata={"card_number": "12345678901"})
    monkeypatch.setattr(snapshot_mod, "list_cards", lambda *a: [card])

    class NoPeople:
        def list_people(self):
            return []

    assert build_snapshot(NoPeople(), None, "tpl").ag_cards_by_site_card[("", "12345678901")] is card
