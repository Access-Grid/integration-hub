"""A user deleted in DMP System Link has their AccessGrid pass deleted.

Seen live (Nov 2026): the user was deleted, a panel compare had stamped them
with everyone else, and the hub kept reading them as current — so their pass
stayed. Runs the real engine against a real encrypted Users.dat.
"""

from __future__ import annotations

from datetime import datetime

from agsync.lib.pacs.dmp.adapter import DmpAdapter
from agsync.sync import engine as engine_mod
from agsync.sync import tracking
from agsync.sync.engine import SyncEngine

from .dmp_dbisam import user, users_dat
from .test_unity_spec_end_to_end import FakeAccessGrid, mailed  # noqa: F401

COMPARED = datetime(2026, 11, 23, 13, 24)
ALICE = user(1, "100", "ALICE", COMPARED, "alice@example.com")
BOB = user(2, "200", "BOB", COMPARED, "bob@example.com")


def _world(tmp_path, monkeypatch):
    ag = FakeAccessGrid("desfire", False)
    monkeypatch.setattr(engine_mod.AccessGridConfig, "load", lambda: {
        "account_id": "acct", "api_secret": "s", "template_id": "tpl", "site_code": "239"})
    monkeypatch.setattr(engine_mod.PacsConfig, "load", lambda: {
        "vendor": "dmp", "options": {}, "params": {"db_path": str(tmp_path), "encryption_key": "k"}})
    monkeypatch.setattr(engine_mod, "build_ag_client", lambda *a: ag)
    monkeypatch.setattr(engine_mod, "build_pacs_adapter", lambda vendor, params: DmpAdapter(**params))
    engine = SyncEngine()

    def cycle(records, live):
        (tmp_path / "Users.dat").write_bytes(users_dat(records, live=live))
        result = engine._run_one_cycle_locked()
        assert result.error is None, result.error

    return ag, cycle


def _pass(employee):
    [row] = [t for t in tracking.all_tracked() if t.pacs_person_id == employee]
    return row


def test_deleting_a_user_in_dmp_deletes_their_pass(tmp_path, monkeypatch, mailed):  # noqa: F811
    ag, cycle = _world(tmp_path, monkeypatch)
    cycle([(0, ALICE), (0, BOB)], live=2)
    bobs = _pass("200").ag_card_id
    assert bobs and _pass("100").ag_card_id

    # Deleted in System Link: the record stays, dates and all; its status
    # byte and the header's live count change.
    cycle([(0, ALICE), (1, BOB)], live=1)
    assert ag.verbs("delete") == [("delete", bobs)]
    assert _pass("200").status == "deleted"
    assert _pass("100").status != "deleted" and ag.passes[_pass("100").ag_card_id][0]["state"] != "deleted"


def test_nothing_is_deleted_when_the_status_bytes_cannot_be_trusted(tmp_path, monkeypatch, mailed):  # noqa: F811
    ag, cycle = _world(tmp_path, monkeypatch)
    cycle([(0, ALICE), (0, BOB)], live=2)
    # The header says one record went, but no status byte says which: the
    # dates decide, and with both current they keep both — and say so.
    cycle([(0, ALICE), (0, BOB)], live=1)
    assert ag.verbs("delete") == []
    assert [key for key, _ in mailed] == ["dmp-undetected-deletions"]
