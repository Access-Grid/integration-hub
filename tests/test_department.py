"""A cardholder's department reaches their pass, and follows them.

Sent when the pass is issued and pushed when it changes, Seos or not, and
compared against what the tracking table says was last sent. A pass issued
before departments were sent (its recorded department is NULL) is sent the
person's department once, if they have one.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agsync.db.connection import get_db
from agsync.lib.pacs.base import Credential, CredentialStatus, Person
from agsync.sync import tracking
from agsync.sync.phases import phase1_provision, phase5_retries, phase6_field_changes
from agsync.sync.snapshot import Snapshot
from tests.test_phase6_field_changes import FakeAG, _person, _run, _tracked, recorded  # noqa: F401

# --- issue -------------------------------------------------------------------


class _Cards:
    def __init__(self):
        self.calls: list[dict] = []

    def provision(self, **params):
        self.calls.append(params)
        return SimpleNamespace(id="pass-1", state="active")


def _issue(monkeypatch, department, allocate=False):
    rows: list[dict] = []
    t = phase1_provision.tracking
    monkeypatch.setattr(t, "get", lambda *a, **k: None)
    monkeypatch.setattr(t, "upsert", lambda **kw: rows.append(kw))
    monkeypatch.setattr(t, "record_error", lambda *a, **k: None)
    snap = Snapshot()
    snap.people["p1"] = Person(id="p1", full_name="Jim Cooper", email="jim@x.test", department=department)
    snap.credentials_by_person["p1"] = [Credential(id="c1", person_id="p1", card_number="42069", site_code="239",
                                                   status=CredentialStatus.ACTIVE, trigger_active=True,
                                                   allocate_identity=allocate)]
    ag = SimpleNamespace(access_cards=_Cards())
    phase1_provision.run(snap, ag, "tpl")
    return ag.access_cards.calls, rows


@pytest.mark.parametrize("allocate", [False, True], ids=["desfire", "seos"])
def test_a_pass_is_issued_with_the_department_and_it_is_recorded(monkeypatch, allocate):
    [params], rows = _issue(monkeypatch, "Mesa High - 271", allocate)
    assert params["department"] == "Mesa High - 271"
    assert [r["last_synced_department"] for r in rows] == ["Mesa High - 271", "Mesa High - 271"]


def test_no_department_sends_none(monkeypatch):
    [params], rows = _issue(monkeypatch, "")
    assert "department" not in params and rows[-1]["last_synced_department"] == ""


def test_a_retried_issue_sends_the_department_too(monkeypatch):
    sent: list[dict] = []
    row = SimpleNamespace(pacs_person_id="p1", pacs_credential_id="c1", status="error", retry_count=0,
                          ag_card_id=None)
    t = phase5_retries.tracking
    monkeypatch.setattr(t, "failed_records", lambda limit: [row])
    monkeypatch.setattr(t, "upsert", lambda **kw: sent.append(kw))
    snap = Snapshot()
    snap.people["p1"] = Person(id="p1", full_name="Jim Cooper", email="jim@x.test", department="Coaches")
    snap.credentials_by_person["p1"] = [Credential(id="c1", person_id="p1", card_number="42069", site_code="239",
                                                   status=CredentialStatus.ACTIVE, trigger_active=True)]
    ag = SimpleNamespace(access_cards=_Cards())
    phase5_retries.run(snap, ag, "tpl")
    assert ag.access_cards.calls[0]["department"] == "Coaches"
    assert sent[0]["last_synced_department"] == "Coaches"


# --- updates -----------------------------------------------------------------


@pytest.mark.parametrize("allocate", [False, True], ids=["desfire", "seos"])
def test_a_changed_department_is_pushed_and_recorded(monkeypatch, recorded, allocate):  # noqa: F811
    ag = FakeAG()
    snap_person = _person(department="Substitute Services - 741")
    monkeypatch.setattr(phase6_field_changes.tracking, "all_tracked",
                        lambda: [_tracked(last_synced_department="Mesa High - 271")])
    snap = Snapshot()
    snap.people[snap_person.id] = snap_person
    snap.credentials_by_person[snap_person.id] = [Credential(id="seos", person_id=snap_person.id,
                                                             allocate_identity=allocate, trigger_active=True)]
    assert phase6_field_changes.run(snap, ag) == 1
    assert ag.access_cards.calls[0]["department"] == "Substitute Services - 741"
    assert recorded[0]["department"] == "Substitute Services - 741"


def test_a_cleared_department_is_pushed_as_blank(monkeypatch, recorded):  # noqa: F811
    ag = FakeAG()
    _run(monkeypatch, ag, _person(department=""), _tracked(last_synced_department="Coaches"))
    assert ag.access_cards.calls[0]["department"] == ""


def test_an_unchanged_department_sends_nothing(monkeypatch, recorded):  # noqa: F811
    ag = FakeAG()
    assert _run(monkeypatch, ag, _person(department="Coaches"), _tracked(last_synced_department="Coaches")) == 0
    assert ag.access_cards.calls == []


def test_a_pass_issued_before_departments_were_sent_gets_one_once(monkeypatch, recorded):  # noqa: F811
    ag = FakeAG()
    _run(monkeypatch, ag, _person(department="Coaches"), _tracked(last_synced_department=None))
    assert ag.access_cards.calls[0]["department"] == "Coaches"
    assert recorded[0]["department"] == "Coaches"


def test_a_pass_issued_before_departments_with_none_to_send_is_recorded_not_pushed(monkeypatch, recorded):  # noqa: F811
    ag = FakeAG()
    _run(monkeypatch, ag, _person(department=""), _tracked(last_synced_department=None))
    assert ag.access_cards.calls == []
    assert recorded[0]["department"] == ""


def test_a_failed_department_push_is_not_recorded(monkeypatch, recorded):  # noqa: F811
    _run(monkeypatch, FakeAG(fail=True), _person(department="Coaches"), _tracked(last_synced_department=""))
    assert recorded == []


# --- the tracking table --------------------------------------------------------


def _track(department=""):
    tracking.upsert(pacs_person_id="p", pacs_credential_id="c", ag_card_id="pass", full_name="x",
                    employee_id="p", status="active", last_synced_department=department)


def test_the_tracking_table_keeps_the_department():
    _track("Coaches")
    assert tracking.get("p", "c").last_synced_department == "Coaches"
    tracking.update_field_tracking("p", "c", full_name="x", email="", phone="", title="", department="Office")
    assert tracking.get("p", "c").last_synced_department == "Office"
    tracking.update_field_tracking("p", "c", full_name="x", email="", phone="", title="")
    assert tracking.get("p", "c").last_synced_department == "Office", "None leaves it as it was"


def test_a_row_from_before_the_column_reads_as_never_sent():
    _track()
    get_db().execute("UPDATE ag_credentials SET last_synced_department = NULL")
    assert tracking.get("p", "c").last_synced_department is None


# --- Avigilon Unity ------------------------------------------------------------


def test_unity_reads_the_department_and_an_unread_person_keeps_the_last_sent(monkeypatch):
    from tests.test_avigilon_unity import FakeUnity, _adapter, _token

    row = SimpleNamespace(pacs_person_id="b", pacs_credential_id="tb", ag_card_id="pass-b", status="active",
                          last_synced_full_name="Ann Lee", last_synced_email="ann@x.test", last_synced_phone="",
                          last_synced_title="", last_synced_department="Coaches")
    monkeypatch.setattr(tracking, "all_tracked", lambda: [row])
    fake = FakeUnity([{"cn": "a", "plasecName": "Cooper, Jim", "plasecIdstatus": "Active",
                       "plasecidentityEmailaddress": "jim@x.test", "plasecidentityDepartment": "Mesa High - 271"}],
                     tokens={"a": [_token("t1")]})
    fake.fail["/identities/b.json"] = 500  # b is tracked but cannot be read this cycle
    people = {p.id: p for p in _adapter(fake).list_people()}
    assert people["a"].department == "Mesa High - 271"
    assert people["b"].department == "Coaches", "an unread person must not read as a cleared department"
