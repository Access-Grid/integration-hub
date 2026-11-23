"""DMP adapter: registration, mapping, email-field trigger, read-only, live rows."""

from __future__ import annotations

from datetime import datetime, timedelta

from agsync.lib.pacs import available_pacs, build_adapter
from agsync.lib.pacs.base import CredentialStatus
from agsync.lib.pacs.dmp.adapter import DmpAdapter
from agsync.lib.pacs.dmp.client import DmpClient


class _FakeClient:
    def __init__(self, rows):
        self._rows = rows

    def read_users(self):
        return self._rows

    def test_connection(self):
        return True, "ok"


def _adapter(rows, email_field="U_FIELD1"):
    a = DmpAdapter(db_path="x", encryption_key="k", email_field=email_field)
    a._client = _FakeClient(rows)
    return a


def _user(code, name, active=True, **extra):
    row = {"USER_NUM": 1, "CODE": code, "NAME": name, "ACTIVE": active,
           "DEPARTMENT": "", "U_FIELD1": "", "U_FIELD2": "", "U_FIELD3": ""}
    row.update(extra)
    return row


# --- registration --------------------------------------------------------


def test_dmp_is_registered():
    assert "dmp" in {d.vendor for d in available_pacs()}
    a = build_adapter("dmp", {"db_path": "x", "encryption_key": "k"})
    assert isinstance(a, DmpAdapter)


# --- mapping -------------------------------------------------------------


def test_person_and_credential_mapping():
    a = _adapter([_user("45001", "ALEX MORGAN", DEPARTMENT="OPS")])
    people = list(a.list_people())
    assert len(people) == 1
    p = people[0]
    assert p.id == "45001"
    assert p.full_name == "ALEX MORGAN"
    assert p.active is True
    assert p.department == "OPS"

    creds = list(a.list_credentials("45001"))
    assert len(creds) == 1
    c = creds[0]
    assert c.card_number == "45001"
    assert c.person_id == "45001"
    assert c.site_code == ""  # DMP discards facility code; global one applies
    assert c.status is CredentialStatus.ACTIVE


def test_inactive_user_credential_is_suspended():
    a = _adapter([_user("45002", "SAM RIVERA", active=False)])
    list(a.list_people())
    c = list(a.list_credentials("45002"))[0]
    assert c.status is CredentialStatus.SUSPENDED


# --- enrollment trigger = email in the configured User Field -------------


def test_trigger_active_when_email_present():
    a = _adapter([_user("45001", "ALEX", U_FIELD1="alex@example.com")])
    list(a.list_people())
    c = list(a.list_credentials("45001"))[0]
    assert c.trigger_active is True
    assert list(a.list_people())[0].email == "alex@example.com"


def test_no_trigger_without_email():
    a = _adapter([_user("45001", "ALEX")])
    list(a.list_people())
    c = list(a.list_credentials("45001"))[0]
    assert c.trigger_active is False


def test_email_field_is_configurable():
    a = _adapter(
        [_user("45001", "ALEX", U_FIELD2="t@example.com")], email_field="U_FIELD2"
    )
    p = list(a.list_people())[0]
    assert p.email == "t@example.com"
    assert list(a.list_credentials("45001"))[0].trigger_active is True


# --- read-only -----------------------------------------------------------


def test_read_only_writeback():
    a = _adapter([_user("45001", "ALEX")])
    assert a.supports_status_writeback is False
    assert a.update_credential_status("45001", "45001", CredentialStatus.SUSPENDED) is False


# --- live vs deleted row filtering --------------------------------------


def test_live_rows_drops_ghosts_and_dedups_reused_slots():
    client = DmpClient(db_path="x", encryption_key="k")
    recent = datetime(2026, 7, 21, 14, 25, 20)
    stale = datetime(2025, 7, 9, 8, 49, 53)
    rows = [
        # a normal live user (in the latest compare cohort)
        {"USER_NUM": 10, "CODE": "100", "NAME": "LIVE ONE",
         "LAST_PNL_CMP": recent, "LAST_CHANGE": recent - timedelta(days=30)},
        # a standalone ghost: old panel-compare, never re-touched
        {"USER_NUM": 20, "CODE": "200", "NAME": "GHOST",
         "LAST_PNL_CMP": stale, "LAST_CHANGE": stale},
        # a reused slot 30: live occupant (recent) + ghost prior occupant (stale)
        {"USER_NUM": 30, "CODE": "301", "NAME": "REUSED LIVE",
         "LAST_PNL_CMP": recent, "LAST_CHANGE": recent - timedelta(days=10)},
        {"USER_NUM": 30, "CODE": "302", "NAME": "REUSED GHOST",
         "LAST_PNL_CMP": stale, "LAST_CHANGE": stale},
        # a brand-new user added after the last compare (no compare yet)
        {"USER_NUM": 40, "CODE": "400", "NAME": "NEW",
         "LAST_PNL_CMP": None, "LAST_CHANGE": recent + timedelta(hours=1)},
    ]
    live = {r["CODE"]: r for r in client._live_rows(rows)}
    assert set(live) == {"100", "301", "400"}
    assert "200" not in live  # standalone ghost dropped
    assert "302" not in live  # prior occupant of reused slot dropped


def test_live_rows_fallback_when_never_compared():
    client = DmpClient(db_path="x", encryption_key="k")
    rows = [
        {"USER_NUM": 1, "CODE": "100", "NAME": "A", "LAST_PNL_CMP": None, "LAST_CHANGE": None},
        {"USER_NUM": 2, "CODE": "200", "NAME": "B", "LAST_PNL_CMP": None, "LAST_CHANGE": None},
    ]
    # No panel-compare data at all → can't distinguish, keep all valid rows.
    assert {r["CODE"] for r in client._live_rows(rows)} == {"100", "200"}


# --- deleted users ---------------------------------------------------------
# A user deleted in System Link keeps its bytes in Users.dat, dates and all.
# Each record's status byte says whether it is live, checked against the
# header's exact live count; only where they disagree do the dates decide, and
# those cannot see a user deleted since the last compare.

def _users_dat(tmp_path, live, bob_status=0):
    from .dmp_dbisam import user, users_dat

    compared = datetime(2026, 11, 16, 9)
    (tmp_path / "Users.dat").write_bytes(users_dat(
        [(0, user(1, "100", "ALICE", compared, "a@x.co")), (bob_status, user(2, "200", "BOB", compared, "b@x.co"))],
        live=live,
    ))
    return DmpAdapter(db_path=str(tmp_path), encryption_key="k")


def test_a_deleted_user_is_not_read_even_with_current_dates(tmp_path, caplog, monkeypatch):
    from agsync import notifications

    mailed = []
    monkeypatch.setattr(notifications, "notify_card_problem", lambda key, problem: mailed.append(key))
    adapter = _users_dat(tmp_path, live=1, bob_status=1)  # BOB deleted since the last compare
    assert {p.id for p in adapter.list_people()} == {"100"}
    assert adapter._client.live_by == "status"
    assert "panel-compare dates" not in caplog.text and mailed == []


def test_each_record_is_decrypted_from_iv_zero(tmp_path):
    # Decrypted as one run, each record's first 8 bytes came out scrambled by
    # the record before it: the status byte was noise, never 0 or 1.
    _users_dat(tmp_path, live=1, bob_status=1)
    from pydbisam import PyDBISAM

    db = PyDBISAM(data=DmpClient(str(tmp_path), "k")._decrypt(tmp_path / "Users.dat"))
    statuses = [db._data[db._data_offset + i * db._row_size] for i in range(2)]
    ids = [int.from_bytes(db._data[db._data_offset + i * db._row_size + 1:][:4], "little") for i in range(2)]
    assert statuses == [0, 1] and ids == [1, 2]


def test_status_bytes_that_disagree_with_the_header_fall_back_to_dates(tmp_path, caplog, monkeypatch):
    from agsync import notifications

    mailed = []
    monkeypatch.setattr(notifications, "notify_card_problem", lambda key, problem: mailed.append((key, problem)))
    adapter = _users_dat(tmp_path, live=1)  # both marked live; the header says one is
    assert {p.id for p in adapter.list_people()} == {"100", "200"}, "the dates alone still read BOB as current"
    assert adapter._client.live_by == "dates"
    assert "2 record(s) are marked live but the header says 1" in caplog.text
    assert "1 deleted user(s) still read as current" in caplog.text
    assert [r.levelname for r in caplog.records if "deleted user" in r.getMessage()] == ["ERROR"]
    [(key, problem)] = mailed
    assert key == "dmp-undetected-deletions" and "panel compare" in problem


def test_nothing_reported_when_the_header_agrees(tmp_path, caplog, monkeypatch):
    from agsync import notifications

    mailed = []
    monkeypatch.setattr(notifications, "notify_card_problem", lambda key, problem: mailed.append(key))
    adapter = _users_dat(tmp_path, live=2)
    assert len(list(adapter.list_people())) == 2
    assert adapter._client.undetected_deletions == 0 and adapter._client.live_by == "status"
    assert "header" not in caplog.text and mailed == []
