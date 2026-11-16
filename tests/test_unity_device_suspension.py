"""Device restrictions survive whole-pass changes, failed writes and restart."""

import sqlite3

import pytest

from agsync.db import connection, schema
from agsync.lib.pacs.avigilon import written
from agsync.lib.pacs.avigilon.client import UnityClient, UnityError
from agsync.sync.engine import SyncEngine

from .test_avigilon_unity import _token
from .test_unity_spec_end_to_end import (  # noqa: F401 — shared engine fixtures
    _seos,
    _status,
    _tokens_by_number,
)
from .test_unity_spec_end_to_end import (
    mailed as _mailed,
)
from .test_unity_spec_end_to_end import (
    world as _world,
)

mailed, world = _mailed, _world


def _devices(world, pair=True):
    unity, ag, cycle = _seos(world, pair)
    cycle()
    pass_id = ag.only_pass()
    ag.install(pass_id)
    cycle()
    phone = next(c for c in ag.passes[pass_id] if c["state"] == "active")
    phone["devices"].append({"device_type": "apple_watch", "status": "installed",
                             "site_code": "239", "card_number": "9555"})
    cycle()
    assert set(_tokens_by_number(unity).values()) == {"Active"}
    return unity, ag, cycle, pass_id


def _restart():
    """New connection and engine: nothing in adapter memory carries the policy."""
    connection._local.conn.close()
    del connection._local.conn
    engine = SyncEngine()

    def cycle():
        result = engine._run_one_cycle_locked()
        assert result.error is None, result.error
    return cycle


@pytest.mark.parametrize("pair", [False, True])
@pytest.mark.parametrize("disabled_device", [0, 1], ids=["phone", "watch"])
def test_one_device_can_be_disabled_without_suspending_its_peer(world, pair, disabled_device):
    unity, ag, cycle, pass_id = _devices(world, pair)
    _status(unity.tokens["a"][disabled_device], "Inactive")
    expected = _tokens_by_number(unity)
    for _ in range(3):
        cycle()
        assert _tokens_by_number(unity) == expected
    assert ag.verbs("suspend", "resume", "delete") == []
    assert all(c["state"] == "active" for c in ag.passes[pass_id] if c["state"] != "deleted")


@pytest.mark.parametrize("pair", [False, True])
def test_whole_pass_resume_preserves_device_block_after_restart(world, pair):
    unity, ag, cycle, pass_id = _devices(world, pair)
    phone, watch = unity.tokens["a"]
    _status(phone, "Inactive")
    # Untracked tokens must survive every pass-level change untouched.
    untracked = _token("physical", internal="7777", embossed="physical-card")
    unity.tokens["a"].append(untracked)
    cycle()
    ag.set_state(pass_id, "suspended")
    cycle()
    assert watch["attributes"]["extended_attributes"]["token_status"] == "Inactive"
    cycle = _restart()
    ag.set_state(pass_id, "active")
    for _ in range(3):
        cycle()
    assert phone["attributes"]["extended_attributes"]["token_status"] == "Inactive"
    assert watch["attributes"]["extended_attributes"]["token_status"] == "Active"
    assert untracked == _token("physical", internal="7777", embossed="physical-card")
    assert any(t is untracked for t in unity.tokens["a"])
    assert ag.verbs("suspend", "resume", "delete") == []
    _status(phone, "Active")
    cycle()
    assert not any(e["operator_disabled"] for e in written.all_tokens())


def test_all_devices_off_suspends_pair_and_one_enabled_device_resumes_it(world):
    unity, ag, cycle, pass_id = _devices(world)
    for token in unity.tokens["a"]:
        _status(token, "Inactive")
    cycle()
    cycle()
    assert ag.verbs("suspend", "resume") == [("suspend", pass_id)]
    _status(unity.tokens["a"][1], "Active")
    cycle()
    cycle()
    assert ag.verbs("suspend", "resume") == [("suspend", pass_id), ("resume", pass_id)]
    assert set(_tokens_by_number(unity).values()) == {"Active", "Inactive"}


@pytest.mark.parametrize("accepted_before_error", [False, True])
@pytest.mark.parametrize("phone_disabled", [False, True])
def test_failed_whole_pass_suspend_retries_without_inventing_a_device_block(
    world, monkeypatch, accepted_before_error, phone_disabled,
):
    unity, ag, cycle, pass_id = _devices(world)
    phone, watch = unity.tokens["a"]
    if phone_disabled:
        _status(phone, "Inactive")
    cycle()
    write = UnityClient.set_token_status

    def fail(self, token, status):
        if token["id"] == watch["id"]:
            if accepted_before_error:
                write(self, token, status)
            raise UnityError("response lost")
        return write(self, token, status)

    ag.set_state(pass_id, "suspended")
    with monkeypatch.context() as patch:
        patch.setattr(UnityClient, "set_token_status", fail)
        cycle()
    watch_record = next(e for e in written.all_tokens() if e["token_id"] == watch["id"])
    assert watch_record["pending_active"] is False
    cycle = _restart()
    cycle()
    assert set(_tokens_by_number(unity).values()) == {"Inactive"}
    ag.set_state(pass_id, "active")
    cycle()
    cycle()
    assert phone["attributes"]["extended_attributes"]["token_status"] == ("Inactive" if phone_disabled else "Active")
    assert watch["attributes"]["extended_attributes"]["token_status"] == "Active"
    records = {e["token_id"]: e for e in written.all_tokens()}
    assert records[phone["id"]]["operator_disabled"] is phone_disabled
    assert not records[watch["id"]]["operator_disabled"]
    assert all(e["pending_active"] is None for e in records.values())


def test_missing_device_does_not_suspend_a_peer_that_is_still_active(world):
    unity, ag, cycle, _ = _devices(world)
    unity.tokens["a"].pop()
    cycle()
    cycle()
    assert ag.verbs("suspend", "resume", "delete") == []
    assert set(_tokens_by_number(unity).values()) == {"Active"}


@pytest.mark.parametrize("pair", [False, True])
def test_a_deleted_device_token_is_left_alone_and_the_pass_keeps_syncing(world, mailed, pair):
    from agsync.sync import tracking

    unity, ag, cycle, pass_id = _devices(world, pair)
    watch = unity.tokens["a"].pop()  # an operator deletes the watch's token
    mailed.clear()
    posts = len(unity.posts)
    for _ in range(4):
        cycle()
    assert len(unity.posts) == posts, "no status write every cycle"
    assert mailed == [], "no 'no longer in Unity' mail every cycle"
    assert watch["id"] in {e["token_id"] for e in written.all_tokens()}, "its card is not written again"
    assert len(unity.tokens["a"]) == 1

    ag.set_state(pass_id, "suspended")
    cycle()
    cycle()
    [row] = [t for t in tracking.all_tracked() if t.pacs_person_id == "a"]
    assert (row.last_known_ag_state, row.status) == ("suspended", "suspended"), "the pass's state is recorded"
    assert set(_tokens_by_number(unity).values()) == {"Inactive"}
    ag.set_state(pass_id, "active")
    cycle()
    cycle()
    [row] = [t for t in tracking.all_tracked() if t.pacs_person_id == "a"]
    assert (row.last_known_ag_state, row.status) == ("active", "active")
    assert set(_tokens_by_number(unity).values()) == {"Active"}
    assert len(unity.tokens["a"]) == 1, "the deleted token is never recreated"


@pytest.mark.parametrize("accepted_before_error", [False, True])
def test_failed_resume_retries_only_the_device_that_was_not_independently_disabled(
    world, monkeypatch, accepted_before_error,
):
    unity, ag, cycle, pass_id = _devices(world)
    phone, watch = unity.tokens["a"]
    _status(phone, "Inactive")
    cycle()
    ag.set_state(pass_id, "suspended")
    cycle()
    write = UnityClient.set_token_status

    def fail(self, token, status):
        if token["id"] == watch["id"]:
            if accepted_before_error:
                write(self, token, status)
            raise UnityError("resume response lost")
        return write(self, token, status)

    ag.set_state(pass_id, "active")
    with monkeypatch.context() as patch:
        patch.setattr(UnityClient, "set_token_status", fail)
        cycle()
    assert next(e for e in written.all_tokens() if e["token_id"] == watch["id"])["pending_active"] is True
    cycle = _restart()
    cycle()
    cycle()
    assert phone["attributes"]["extended_attributes"]["token_status"] == "Inactive"
    assert watch["attributes"]["extended_attributes"]["token_status"] == "Active"
    assert all(e["pending_active"] is None for e in written.all_tokens())
    assert ag.verbs("suspend", "resume", "delete") == []


def test_dry_run_never_persists_device_blocks_or_status_intents(world, monkeypatch):
    from agsync import dryrun

    unity, ag, cycle, pass_id = _devices(world)
    _status(unity.tokens["a"][0], "Inactive")
    before = written.all_tokens()
    posts = list(unity.posts)
    monkeypatch.setattr(dryrun, "active", lambda: True)
    ag.set_state(pass_id, "suspended")
    cycle()
    cycle()
    assert written.all_tokens() == before
    assert unity.posts == posts
    assert set(_tokens_by_number(unity).values()) == {"Active", "Inactive"}


def test_migration_preserves_existing_token_ownership_and_status(monkeypatch):
    db = sqlite3.connect(":memory:")
    try:
        migrations = schema.MIGRATIONS
        with monkeypatch.context() as patch:
            patch.setattr(schema, "MIGRATIONS", migrations[:-1])
            schema.apply_migrations(db)
        db.execute("INSERT INTO unity_tokens (pacs_person_id, pacs_credential_id, token_id, "
                   "facility_code, card_number, active) VALUES ('a', 'enrollment', 'known', 'fc', 'cn', 1)")
        schema.apply_migrations(db)
        assert db.execute("SELECT token_id, active, operator_disabled, pending_active FROM unity_tokens").fetchone() == (
            "known", 1, 0, None,
        )
    finally:
        db.close()
