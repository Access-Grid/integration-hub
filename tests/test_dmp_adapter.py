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
