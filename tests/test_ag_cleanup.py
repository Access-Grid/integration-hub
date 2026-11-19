"""ag-cleanup against the state the live box was left in (Nov 2026).

The hub created a Seos pair's first token in Unity but never recorded its ID,
the holder installed on iOS (AccessGrid deleted the Android half), and from
then on every cycle held the pass: enrollment kept, nothing retired. The
stranded state is built here through real engine cycles, then the tool runs
against the same database file, then the hub is expected to finish the job.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agsync import cleanup
from agsync.config import get_settings
from agsync.lib.pacs.avigilon import written

from .test_avigilon_unity import FakeUnity, _client, _token
from .test_unity_spec_end_to_end import _person, _tokens_by_number, mailed, world  # noqa: F401


def _stranded(world, monkeypatch, installed="ios"):  # noqa: F811
    unity = FakeUnity([_person("a")], tokens={"a": [_token("t1", internal="")]})
    ag, cycle = world("seos", True, unity)
    with monkeypatch.context() as patch:
        def lost(row, token_id):
            raise RuntimeError("token created, ID never recorded")
        patch.setattr(written, "set_token", lost)
        cycle()
    pass_id = ag.only_pass()
    ios, android = (c["card_number"] for c in ag.passes[pass_id])
    [reserved] = written.all_tokens()
    assert reserved["token_id"] is None
    ag.install(pass_id, installed)
    cycle()
    cycle()
    live, dead = (ios, android) if installed == "ios" else (android, ios)
    if reserved["card_number"] == live:
        # The live box: the live half's token is the stranded one, so the
        # enrollment stays on, the token stays off, and nothing moves.
        assert _tokens_by_number(unity) == {"": "Active", live: "Inactive"}
    else:
        # The stranded token is the deleted half's: the hub writes the live
        # half on its own, but never retires the token it cannot prove it made.
        assert _tokens_by_number(unity) == {live: "Active", dead: "Inactive"}
    [stuck] = [e for e in written.all_tokens() if e["card_number"] == reserved["card_number"]]
    assert stuck["token_id"] is None
    return unity, ag, cycle, live, dead, reserved


def _run(unity, answer=True):
    from agsync.settings_store import PacsConfig
    PacsConfig.save("avigilon", {"host": "unity", "username": "u", "password": "p"})
    out: list[str] = []
    code = cleanup.run(Path(get_settings().db_path), get_settings().encryption_key,
                       lambda params: _client(unity), lambda n: answer, out.append)
    return code, "\n".join(out)


@pytest.mark.parametrize("installed", ["ios", "android"])
def test_cleanup_adopts_the_stranded_token_and_the_hub_finishes_the_pass(world, monkeypatch, mailed, installed):  # noqa: F811
    """Either way round: the stranded token is the live half's (kept, switched
    on) or the deleted half's (retired, and the live half's token written)."""
    unity, ag, cycle, live, dead, reserved = _stranded(world, monkeypatch, installed)
    assert reserved["card_number"] in (live, dead)
    [made] = [t for t in unity.tokens["a"] if t["attributes"]["plasecInternalnumber"] == reserved["card_number"]]

    code, out = _run(unity)
    assert code == 0, out
    assert f"ADOPT token {made['id']}" in out
    assert next(e for e in written.all_tokens() if e["row"] == reserved["row"])["token_id"] == made["id"]
    assert list(Path(get_settings().db_path).parent.glob("*.before-ag-cleanup-*")), "backed up first"

    cycle()
    cycle()
    assert _tokens_by_number(unity) == {live: "Active"}, "live half on; deleted half's token retired"
    assert {e["card_number"] for e in written.all_tokens()} == {live}
    assert all(t["id"] != "t1" for t in unity.tokens["a"]), "enrollment token deleted"
    assert ag.verbs("suspend", "resume", "delete") == []


def test_cleanup_without_confirmation_changes_nothing(world, monkeypatch, mailed):  # noqa: F811
    unity, *_ = _stranded(world, monkeypatch)
    before = written.all_tokens()
    code, out = _run(unity, answer=False)
    assert code == 2 and "Nothing was changed" in out
    assert written.all_tokens() == before
    assert not list(Path(get_settings().db_path).parent.glob("*.before-ag-cleanup-*"))


def test_cleanup_leaves_alone_a_number_two_unrecorded_tokens_carry(world, monkeypatch, mailed):  # noqa: F811
    unity, ag, cycle, live, dead, reserved = _stranded(world, monkeypatch)
    unity.tokens["a"].append(_token("operator-copy", internal=reserved["card_number"], embossed="AccessGrid",
                                    status="Inactive"))
    code, out = _run(unity)
    assert code == 2 and "LEFT ALONE" in out and "Nothing can be adopted" in out
    assert written.all_tokens()[0]["token_id"] is None


def test_cleanup_never_adopts_the_enrollment_or_a_recorded_token(world, monkeypatch, mailed):  # noqa: F811
    unity, ag, cycle, live, dead, reserved = _stranded(world, monkeypatch)
    for t in unity.tokens["a"]:
        if t["attributes"]["plasecInternalnumber"] == reserved["card_number"]:
            t["attributes"]["plasecEmbossednumber"] = "badge"  # no longer the hub's marker
    unity.tokens["a"][0]["attributes"]["plasecInternalnumber"] = reserved["card_number"]  # the enrollment, t1
    code, out = _run(unity)
    assert code == 2 and "no unrecorded token" in out
    assert written.all_tokens()[0]["token_id"] is None


def test_cleanup_with_nothing_stranded_says_so_without_reading_unity(tmp_path):
    db = Path(get_settings().db_path)
    from agsync.db.connection import get_db
    get_db()  # migrated, empty
    code = cleanup.run(db, get_settings().encryption_key,
                            lambda params: pytest.fail("Unity read"), lambda n: pytest.fail("asked"), lambda s: None)
    assert code == 0


def test_cleanup_stops_without_a_database(tmp_path):
    with pytest.raises(cleanup.CleanupError, match="no database"):
        cleanup.run(tmp_path / "missing.db", "k", None, None)


def test_cleanup_refuses_another_vendor(tmp_path):
    from agsync.settings_store import PacsConfig
    PacsConfig.save("cdvi", {"host": "x"})
    conn = sqlite3.connect(get_settings().db_path)
    conn.row_factory = sqlite3.Row
    with pytest.raises(cleanup.CleanupError, match="not Avigilon Unity"):
        cleanup.unity_params(conn, get_settings().encryption_key)


def test_main_without_a_database_stops_and_says_nothing_changed(tmp_path, capsys):
    """What the release build's smoke check runs against the built exe."""
    assert cleanup.main(["--db", str(tmp_path / "no-such.db")]) == 1
    assert "nothing changed" in capsys.readouterr().out
