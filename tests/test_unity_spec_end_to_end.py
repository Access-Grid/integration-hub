"""Avigilon Unity against the written spec, cycle by cycle, through the real engine.

Each test runs SyncEngine cycles — all six phases in engine order — over
the real Unity adapter and client (Unity itself faked over HTTP), the real
tracking database, and a fake AccessGrid that behaves as the real one does
where it matters:

- a template pair is issued as one pass (the pair id) and listed only as
  its two halves, never by the pair id;
- the listing carries no devices; only fetching one card says where it is
  installed;
- suspend, resume, delete and update act on the pair id, for both halves;
- installing on one platform deletes the other half.

The spec's sections are the test groups: issue, update, suspend/resume
(Unity → AccessGrid), suspend/resume (AccessGrid → Unity), revoke. Pairs are
acted on by the pair id only, never one half.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from accessgrid.client import AccessCard

from agsync import notifications
from agsync.lib.pacs.avigilon import written
from agsync.lib.pacs.avigilon.adapter import AvigilonAdapter
from agsync.sync import engine as engine_mod
from agsync.sync import tracking
from agsync.sync.engine import SyncEngine

from .test_avigilon_unity import FakeUnity, _client, _identity_deleted, _token

# --- AccessGrid ---------------------------------------------------------------


class FakeAccessGrid:
    """AccessGrid for one card template, single or a pair."""

    def __init__(self, protocol: str, pair: bool):
        self.protocol, self.pair = protocol, pair
        self.passes: dict[str, list[dict]] = {}  # pass id -> its cards (halves)
        self.calls: list[tuple] = []  # (verb, card_id or None, params)
        self._numbers = iter(range(9001, 10000))
        self.console = SimpleNamespace(read_template=self._template)
        self.access_cards = self

    def _template(self, template_id):
        made = [SimpleNamespace(id=f"tpl-{p}", protocol=self.protocol, platform=p) for p in ("apple", "android")]
        return made if self.pair else made[0]

    # the API -------------------------------------------------------------

    def _get(self, path, params=None):
        assert path == "/v1/key-cards"
        return {"keys": [dict(c, devices=[]) for cards in self.passes.values() for c in cards], "total_pages": 1}

    def provision(self, **params):
        self.calls.append(("provision", None, params))
        pass_id = f"pass-{len(self.passes) + 1}"
        cards = []
        for platform in ("ios", "android") if self.pair else ("ios",):
            if self.protocol == "seos":
                site, number = "239", str(next(self._numbers))
            else:
                site, number = params.get("site_code"), params.get("card_number")
            cards.append({
                "id": f"{pass_id}-{platform}" if self.pair else pass_id, "state": "created", "platform": platform,
                "employee_id": params["employee_id"], "full_name": params["full_name"],
                "metadata": dict(params["metadata"]), "site_code": site, "card_number": number, "devices": [],
            })
        self.passes[pass_id] = cards
        if not self.pair:
            return AccessCard(self, dict(cards[0]))
        return AccessCard(self, {"id": pass_id, "state": "created", "details": [
            {"device_type": c["platform"], "site_code": c["site_code"], "card_number": c["card_number"],
             "state": "created"} for c in cards]})

    def get(self, card_id):
        card = next((c for cards in self.passes.values() for c in cards if c["id"] == card_id), None)
        if card is None:
            raise KeyError(card_id)  # a pair id is not a card
        return AccessCard(self, dict(card))

    def suspend(self, card_id):
        self._act("suspend", card_id, "suspended")

    def resume(self, card_id):
        self._act("resume", card_id, "active")

    def delete(self, card_id):
        self._act("delete", card_id, "deleted")

    def update(self, card_id, update_type, **changes):
        self.calls.append(("update", card_id, changes))
        for card in self._cards(card_id):
            card.update(changes)

    def _act(self, verb, card_id, state):
        self.calls.append((verb, card_id, {}))
        for card in self._cards(card_id):
            if card["state"] != "deleted":
                card["state"] = state

    def _cards(self, card_id):
        assert card_id in self.passes, f"{card_id} is not a pass id — a half was acted on"
        return self.passes[card_id]

    # what a holder or AccessGrid operator does ------------------------------

    def install(self, pass_id, platform="ios"):
        """The holder installs on one platform; AccessGrid deletes the other half."""
        for card in self.passes[pass_id]:
            if card["platform"] == platform:
                card["state"] = "active"
                card["devices"] = [{"device_type": "iphone", "status": "installed"}]
            else:
                card["state"] = "deleted"

    def set_state(self, pass_id, state):
        """An AccessGrid operator (or the holder, for unlink) changes the pass."""
        for card in self.passes[pass_id]:
            if card["state"] != "deleted":
                card["state"] = state

    def verbs(self, *verbs):
        return [(v, cid) for v, cid, _ in self.calls if v in verbs]

    def only_pass(self):
        [pass_id] = self.passes
        return pass_id


# --- Unity --------------------------------------------------------------------


def _person(cn, first="Jim", last="Cooper", status="Active", email="jim@example.com", **fields):
    return {"cn": cn, "plasecName": f"{last}, {first}", "plasecFname": first, "plasecLname": last,
            "plasecIdstatus": status, "plasecidentityEmailaddress": email,
            "plasecidentityPhone": "+15550100", "plasecidentityTitle": "Engineer",
            "plasecidentityDepartment": "Research", **fields}


def _status(token, status):
    token["attributes"]["extended_attributes"]["token_status"] = status


def _tokens_by_number(unity, cn="a"):
    return {t["attributes"]["plasecInternalnumber"]: t["attributes"]["extended_attributes"]["token_status"]
            for t in unity.tokens.get(cn, [])}


# --- the engine ---------------------------------------------------------------


@pytest.fixture
def mailed(monkeypatch):
    sent = []
    monkeypatch.setattr(notifications, "notify_card_problem", lambda key, problem: sent.append((key, problem)))
    return sent


@pytest.fixture
def world(monkeypatch, mailed):
    """world(protocol, pair, unity, **params) -> (AccessGrid, run one engine cycle)."""

    def make(protocol, pair, unity, **params):
        ag = FakeAccessGrid(protocol, pair)
        monkeypatch.setattr(engine_mod.AccessGridConfig, "load", lambda: {
            "account_id": "acct", "api_secret": "s", "template_id": "tpl", "site_code": "239"})
        monkeypatch.setattr(engine_mod.PacsConfig, "load", lambda: {
            "vendor": "avigilon", "options": {},
            "params": {"host": "unity", "username": "u", "password": "p", **params}})
        monkeypatch.setattr(engine_mod, "build_ag_client", lambda *a: ag)

        def build(vendor, built_params):
            adapter = AvigilonAdapter(**built_params)
            adapter._client = _client(unity)
            return adapter

        monkeypatch.setattr(engine_mod, "build_pacs_adapter", build)
        engine = SyncEngine()

        def cycle(may_fail=False):
            result = engine._run_one_cycle_locked()
            assert may_fail or result.error is None, result.error
            return result

        return ag, cycle

    return make


def _row(pid="a", cid=None):
    rows = [t for t in tracking.all_tracked() if t.pacs_person_id == pid and (cid is None or t.pacs_credential_id == cid)]
    assert len(rows) == 1, rows
    return rows[0]


PAIRS = pytest.mark.parametrize("pair", [True, False], ids=["pair", "single"])


# ==============================================================================
# DESFire (not HID Seos)
# ==============================================================================


def _desfire(world, pair, **person):
    unity = FakeUnity([_person("a", **person)], tokens={"a": [_token("t1", internal="5001")]})
    ag, cycle = world("desfire", pair, unity)
    return unity, ag, cycle


@PAIRS
def test_desfire_issue_sends_the_card_and_every_field_once(world, pair):
    unity, ag, cycle = _desfire(world, pair)
    cycle()
    cycle()
    [(_, _, sent)] = [c for c in ag.calls if c[0] == "provision"]
    assert (sent["site_code"], sent["card_number"]) == (239, "5001")
    assert (sent["email"], sent["phone_number"], sent["title"], sent["department"]) == \
        ("jim@example.com", "+15550100", "Engineer", "Research")
    assert sent["full_name"] == "Jim Cooper"
    row = _row()
    assert (row.pacs_credential_id, row.ag_card_id, row.status) == ("t1", ag.only_pass(), "active")


@PAIRS
def test_desfire_issue_needs_an_email_or_a_phone(world, pair):
    unity, ag, cycle = _desfire(world, pair, email="", plasecidentityPhone="")
    cycle()
    assert ag.verbs("provision") == []
    unity.identities[0]["plasecidentityPhone"] = "+15550100"
    cycle()
    assert len(ag.verbs("provision")) == 1


@PAIRS
def test_desfire_card_number_already_on_a_live_pass_is_not_issued_twice(world, pair, mailed):
    unity = FakeUnity([_person("a"), _person("b", first="Ann", email="ann@example.com")],
                      tokens={"a": [_token("t1", internal="5001")], "b": [_token("t2", internal="5001")]})
    ag, cycle = world("desfire", pair, unity)
    cycle()
    cycle()
    assert len(ag.verbs("provision")) == 1
    assert any(key.startswith("duplicate:") for key, _ in mailed)


@PAIRS
def test_desfire_update_pushes_every_changed_field_including_email(world, pair):
    unity, ag, cycle = _desfire(world, pair)
    cycle()
    unity.identities[0].update(plasecidentityTitle="Lead", plasecidentityDepartment="Ops",
                               plasecidentityEmailaddress="jim@new.example.com", plasecFname="James",
                               plasecidentityPhone="+15550199")
    cycle()
    pass_id = ag.only_pass()
    changes = {k: v for verb, cid, c in ag.calls if verb == "update" and cid == pass_id for k, v in c.items()}
    assert changes == {"full_name": "James Cooper", "title": "Lead", "department": "Ops",
                       "phone_number": "+15550199", "email": "jim@new.example.com"}
    row = _row()
    assert (row.last_synced_title, row.last_synced_department, row.last_synced_email) == \
        ("Lead", "Ops", "jim@new.example.com")
    cycle()
    assert len(ag.verbs("update")) == 2, "nothing pushed again"


@PAIRS
def test_desfire_token_switched_off_and_on_in_unity_suspends_and_resumes_the_pass(world, pair):
    unity, ag, cycle = _desfire(world, pair)
    cycle()
    pass_id = ag.only_pass()
    _status(unity.tokens["a"][0], "Inactive")
    cycle()
    cycle()
    assert ag.verbs("suspend", "resume") == [("suspend", pass_id)]
    assert _row().status == "suspended"
    _status(unity.tokens["a"][0], "Active")
    cycle()
    cycle()
    assert ag.verbs("suspend", "resume") == [("suspend", pass_id), ("resume", pass_id)]
    assert _row().status == "active"


@PAIRS
@pytest.mark.parametrize("state", ["suspended", "unlink"])
def test_desfire_pass_switched_off_in_accessgrid_switches_the_token_off_and_back_on(world, pair, state):
    unity, ag, cycle = _desfire(world, pair)
    cycle()
    pass_id = ag.only_pass()
    ag.install(pass_id)
    cycle()
    ag.set_state(pass_id, state)
    cycle()
    cycle()
    assert _tokens_by_number(unity) == {"5001": "Inactive"}
    assert ag.verbs("suspend", "resume") == [], "the PACS change it caused is not pushed back"
    assert _row().status == "suspended"
    ag.set_state(pass_id, "active")
    cycle()
    cycle()
    assert _tokens_by_number(unity) == {"5001": "Active"}
    assert ag.verbs("suspend", "resume") == []


def test_desfire_pair_keeps_the_token_on_when_the_uninstalled_half_is_deleted(world):
    unity, ag, cycle = _desfire(world, True)
    cycle()
    ag.install(ag.only_pass(), "android")  # the iOS half is deleted
    cycle()
    cycle()
    assert _tokens_by_number(unity) == {"5001": "Active"}
    assert ag.verbs("suspend", "resume", "delete") == []


@PAIRS
@pytest.mark.parametrize("how", ["identity deleted", "token deleted", "trigger removed"])
def test_desfire_revoke(world, pair, how):
    unity, ag, cycle = _desfire(world, pair)
    unity.identities.append(_person("z", first="Zed", email="zed@example.com"))  # someone else, untouched
    unity.tokens["z"] = [_token("tz", internal="5009")]
    cycle()
    pass_a = next(p for p, cards in ag.passes.items() if cards[0]["employee_id"] == "a")
    if how == "identity deleted":
        _identity_deleted(unity)
    elif how == "token deleted":
        unity.tokens["a"] = []
    else:
        unity.tokens["a"][0]["attributes"]["plasecEmbossednumber"] = "lost"
    cycle()
    assert ag.verbs("delete") == [("delete", pass_a)]
    assert _row("a").status == "deleted"
    assert _row("z").status == "active"


def test_desfire_card_number_edited_after_issue_updates_the_same_pass(world, mailed):
    unity, ag, cycle = _desfire(world, True)
    cycle()
    unity.tokens["a"][0]["attributes"]["plasecInternalnumber"] = "5002"
    cycle()
    updates = [params for verb, _, params in ag.calls if verb == "update"]
    assert len(updates) == 1 and updates[0]["card_number"] == "5002"
    assert mailed == []
    assert ag.verbs("delete", "provision") == [("provision", None)]


# ==============================================================================
# HID Seos
# ==============================================================================


def _seos(world, pair=True, email="jim@example.com", **params):
    unity = FakeUnity([_person("a", email=email)], tokens={"a": [_token("t1", internal="")]})
    ag, cycle = world("seos", pair, unity, **params)
    return unity, ag, cycle


@PAIRS
def test_seos_issue_sends_no_card_writes_inactive_tokens_and_deletes_the_enrollment(world, pair):
    unity, ag, cycle = _seos(world, pair)
    cycle()
    [(_, _, sent)] = [c for c in ag.calls if c[0] == "provision"]
    assert "card_number" not in sent and "site_code" not in sent and "file_data" not in sent
    assert (sent["email"], sent["phone_number"], sent["title"], sent["department"]) == \
        ("jim@example.com", "+15550100", "Engineer", "Research")
    numbers = [c["card_number"] for c in ag.passes[ag.only_pass()]]
    assert _tokens_by_number(unity) == {n: "Inactive" for n in numbers}
    assert all(t["id"] != "t1" for t in unity.tokens["a"]), "enrollment token deleted"
    cycle()
    assert len(ag.verbs("provision")) == 1
    assert _tokens_by_number(unity) == {n: "Inactive" for n in numbers}, "not installed: stays off"


def test_seos_no_email_and_no_domain_holds_the_issue_and_mails(world, mailed):
    unity, ag, cycle = _seos(world, email="")
    cycle()
    assert ag.verbs("provision") == []
    assert any(key == "a:t1:email" for key, _ in mailed)


def test_seos_no_email_with_a_domain_issues_to_a_synthetic_address(world):
    unity, ag, cycle = _seos(world, email="", email_domain="cards.example.com")
    cycle()
    [(_, _, sent)] = [c for c in ag.calls if c[0] == "provision"]
    assert sent["email"] == "jim.cooper.a@cards.example.com"


def test_seos_install_switches_on_only_the_installed_half(world):
    unity, ag, cycle = _seos(world)
    cycle()
    pass_id = ag.only_pass()
    ios, android = (c["card_number"] for c in ag.passes[pass_id])
    ag.install(pass_id, "ios")
    cycle()
    assert _tokens_by_number(unity) == {ios: "Active"}, "the deleted Android half's token is gone"
    assert android not in {e["card_number"] for e in written.all_tokens()}
    assert ag.verbs("suspend", "resume", "delete") == []


def _installed_seos(world):
    unity, ag, cycle = _seos(world)
    cycle()
    pass_id = ag.only_pass()
    ag.install(pass_id, "ios")
    cycle()
    ios = ag.passes[pass_id][0]["card_number"]
    assert _tokens_by_number(unity) == {ios: "Active"}
    return unity, ag, cycle, pass_id, ios


@pytest.mark.parametrize("state", ["suspended", "unlink"])
def test_seos_pass_switched_off_in_accessgrid_switches_the_token_off_and_back_on(world, state):
    unity, ag, cycle, pass_id, ios = _installed_seos(world)
    ag.set_state(pass_id, state)
    cycle()
    cycle()
    assert _tokens_by_number(unity) == {ios: "Inactive"}
    assert ag.verbs("suspend", "resume") == []
    ag.set_state(pass_id, "active")
    cycle()
    cycle()
    assert _tokens_by_number(unity) == {ios: "Active"}
    assert ag.verbs("suspend", "resume") == []


def test_seos_token_switched_off_and_on_in_unity_suspends_and_resumes_the_pass(world):
    unity, ag, cycle, pass_id, ios = _installed_seos(world)
    [token] = unity.tokens["a"]
    _status(token, "Inactive")
    cycle()
    cycle()
    assert ag.verbs("suspend", "resume") == [("suspend", pass_id)]
    assert _tokens_by_number(unity) == {ios: "Inactive"}, "not switched back on"
    _status(token, "Active")
    cycle()
    cycle()
    assert ag.verbs("suspend", "resume") == [("suspend", pass_id), ("resume", pass_id)]
    assert _tokens_by_number(unity) == {ios: "Active"}


def test_seos_identity_deactivated_suspends_the_pass_and_reactivated_resumes_it(world):
    unity, ag, cycle, pass_id, ios = _installed_seos(world)
    unity.identities[0]["plasecIdstatus"] = "Inactive"
    cycle()
    cycle()
    assert ag.verbs("suspend", "resume") == [("suspend", pass_id)]
    unity.identities[0]["plasecIdstatus"] = "Active"
    cycle()
    cycle()
    assert ag.verbs("suspend", "resume") == [("suspend", pass_id), ("resume", pass_id)]
    assert _tokens_by_number(unity) == {ios: "Active"}


def test_seos_update_never_sends_the_email(world):
    unity, ag, cycle, pass_id, _ = _installed_seos(world)
    unity.identities[0].update(plasecidentityEmailaddress="jim@new.example.com",
                               plasecidentityTitle="Lead", plasecidentityDepartment="Ops")
    cycle()
    changes = {k: v for verb, cid, c in ag.calls if verb == "update" and cid == pass_id for k, v in c.items()}
    assert changes == {"title": "Lead", "department": "Ops"}


def test_seos_revoke_when_the_identity_is_deleted(world):
    """With someone else still readable: a 404 counts as "deleted" only once
    another identity's detail has read, so a box answering 404 for every
    detail revokes nobody."""
    unity, ag, cycle, pass_id, _ = _installed_seos(world)
    unity.identities.append(_person("z", first="Zed", email="zed@example.com"))
    unity.tokens["z"] = [_token("tz", internal="")]
    cycle()
    _identity_deleted(unity)
    cycle()
    assert ag.verbs("delete") == [("delete", pass_id)]


def test_seos_revoke_forgets_the_hubs_tokens_of_a_deleted_identity(world):
    unity, ag, cycle, pass_id, _ = _installed_seos(world)
    unity.identities.append(_person("z", first="Zed", email="zed@example.com"))
    unity.tokens["z"] = [_token("tz", internal="")]
    cycle()
    _identity_deleted(unity)
    cycle()
    cycle()
    assert [e for e in written.all_tokens() if e["person_id"] == "a"] == []


def test_seos_revoke_when_every_hub_token_is_deleted_in_unity(world):
    unity, ag, cycle, pass_id, _ = _installed_seos(world)
    unity.tokens["a"] = []
    cycle()
    assert ag.verbs("delete") == [("delete", pass_id)]


@PAIRS
def test_an_unreadable_template_stops_the_cycle_rather_than_guess_the_direction(world, pair):
    """Read as not-Seos, the hub's own tokens look like new enrollments and the
    deleted enrollment like a credential gone: one failed template read deleted
    every installed Seos pass and issued each holder a new one."""
    unity, ag, cycle = _seos(world, pair)
    cycle()
    pass_id = ag.only_pass()
    ag.install(pass_id)
    cycle()
    cycle()
    calls = list(ag.calls)

    readable = ag.console.read_template

    def unreadable(template_id):
        raise RuntimeError("503 Service Unavailable")

    ag.console.read_template = unreadable
    result = cycle(may_fail=True)
    assert result.error and "template" in result.error
    assert ag.calls == calls
    ag.console.read_template = readable
    cycle()
    assert ag.calls == calls
    row = _row()
    assert (row.ag_card_id, row.status) == (pass_id, "active")


@PAIRS
def test_seos_email_change_alerts_and_metadata_still_updates(world, mailed, pair):
    unity, ag, cycle = _seos(world, pair)
    cycle()
    unity.identities[0].update(plasecidentityEmailaddress="new@example.com", plasecidentityTitle="Lead")
    cycle()
    assert any(key == "email-change:a:t1" for key, _ in mailed)
    updates = [(cid, params) for verb, cid, params in ag.calls if verb == "update"]
    assert updates == [(ag.only_pass(), {"title": "Lead"})]


@PAIRS
def test_seos_card_number_edit_alerts_without_changing_accessgrid(world, mailed, pair):
    unity, ag, cycle = _seos(world, pair)
    cycle()
    token = unity.tokens["a"][0]
    token["attributes"]["plasecInternalnumber"] = "123456"
    cycle()
    assert any(key == "seos-number:a:t1" for key, _ in mailed)
    assert not ag.verbs("update", "provision")[1:]


@PAIRS
def test_seos_entire_pass_deleted_deactivates_tokens_and_keeps_tracking(world, pair):
    unity, ag, cycle = _seos(world, pair)
    cycle()
    pass_id = ag.only_pass()
    ag.install(pass_id)
    cycle()
    ag.set_state(pass_id, "deleted")
    cycle()
    cycle()
    assert set(_tokens_by_number(unity).values()) == {"Inactive"}
    assert (_row().status, _row().ag_card_id) == ("suspended", pass_id)
    assert ag.verbs("delete", "suspend", "resume") == []


@PAIRS
def test_desfire_token_switched_back_on_after_its_pass_was_deleted_issues_a_new_pass(world, pair):
    unity, ag, cycle = _desfire(world, pair)
    cycle()
    old = ag.only_pass()
    ag.install(old)
    cycle()
    ag.set_state(old, "deleted")
    cycle()
    cycle()
    assert _tokens_by_number(unity) == {"5001": "Inactive"}
    unity.identities[0]["plasecidentityTitle"] = "Lead"
    cycle()
    assert [c for c in ag.calls if c[1] == old and c[0] != "get"] == [], "nothing is sent to a deleted pass"

    _status(unity.tokens["a"][0], "Active")
    cycle()
    cycle()
    new = [pid for pid in ag.passes if pid != old]
    assert len(new) == 1 and len(ag.verbs("provision")) == 2
    [(_, _, sent)] = [c for c in ag.calls if c[0] == "provision"][1:]
    assert (sent["site_code"], sent["card_number"], sent["title"]) == (239, "5001", "Lead")
    row = _row()
    assert (row.pacs_credential_id, row.ag_card_id, row.status) == ("t1", new[0], "active")
    assert ag.verbs("delete", "suspend", "resume") == []


@PAIRS
def test_seos_token_switched_back_on_after_its_pass_was_deleted_issues_a_new_pass(world, pair):
    unity, ag, cycle = _seos(world, pair)
    cycle()
    old = ag.only_pass()
    ag.install(old)
    cycle()
    ag.set_state(old, "deleted")
    cycle()
    cycle()
    [old_token] = unity.tokens["a"]
    _status(old_token, "Active")
    cycle()
    cycle()
    new = [pid for pid in ag.passes if pid != old]
    assert len(new) == 1 and len(ag.verbs("provision")) == 2
    new_numbers = {c["card_number"] for c in ag.passes[new[0]]}
    assert _tokens_by_number(unity) == {n: "Inactive" for n in new_numbers}, "the old token is gone"
    assert {e["card_number"] for e in written.all_tokens()} == new_numbers
    [row] = [t for t in tracking.all_tracked() if t.ag_card_id]
    assert (row.pacs_credential_id, row.ag_card_id) == (old_token["id"], new[0])
    assert _row(cid="t1").status == "deleted"
    ag.install(new[0])
    cycle()
    cycle()
    assert list(_tokens_by_number(unity).values()) == ["Active"]


def test_seos_pair_deleted_before_install_reissues_when_one_token_is_switched_on(world):
    unity, ag, cycle = _seos(world, True)
    cycle()
    old = ag.only_pass()
    cycle()
    ag.set_state(old, "deleted")
    cycle()
    cycle()
    assert len(unity.tokens["a"]) == 2 and set(_tokens_by_number(unity).values()) == {"Inactive"}
    _status(unity.tokens["a"][1], "Active")
    cycle()
    cycle()
    new = [pid for pid in ag.passes if pid != old]
    assert len(new) == 1 and len(ag.verbs("provision")) == 2
    new_numbers = {c["card_number"] for c in ag.passes[new[0]]}
    assert _tokens_by_number(unity) == {n: "Inactive" for n in new_numbers}


def test_seos_tokens_of_a_deleted_pass_left_off_issue_nothing(world):
    unity, ag, cycle = _seos(world, True)
    cycle()
    old = ag.only_pass()
    ag.install(old)
    cycle()
    ag.set_state(old, "deleted")
    for _ in range(4):
        cycle()
    assert len(ag.verbs("provision")) == 1
    assert set(_tokens_by_number(unity).values()) == {"Inactive"}


def test_seos_active_but_uninstalled_stays_inactive(world):
    unity, ag, cycle = _seos(world)
    cycle()
    ag.set_state(ag.only_pass(), "active")
    cycle()
    cycle()
    assert set(_tokens_by_number(unity).values()) == {"Inactive"}
    assert ag.verbs("suspend", "resume") == []


def test_seos_active_installed_member_wins_over_suspended_member(world):
    unity, ag, cycle = _seos(world)
    cycle()
    cards = ag.passes[ag.only_pass()]
    cards[0].update(state="active", devices=[{"status": "installed", "device_type": "apple_watch"}])
    cards[1]["state"] = "suspended"
    cycle()
    assert set(_tokens_by_number(unity).values()) == {"Active"}
    assert ag.verbs("suspend", "resume") == []


@PAIRS
def test_desfire_identity_change_updates_payload_by_pass_id(world, pair):
    unity, ag, cycle = _desfire(world, pair)
    cycle()
    unity.tokens["a"][0]["attributes"]["plasecInternalnumber"] = "5002"
    cycle()
    updates = [(cid, params) for verb, cid, params in ag.calls if verb == "update"]
    assert len(updates) == 1
    assert updates[0][0] == ag.only_pass()
    assert updates[0][1]["card_number"] == "5002"
    assert updates[0][1]["metadata"]["card_number"] == "5002"
    cycle()
    assert len([v for v, _, _ in ag.calls if v == "update"]) == 1


def test_seos_failed_revoke_keeps_writeback_records_until_success(world, monkeypatch):
    from agsync.ag import AccessGridError

    unity, ag, cycle = _seos(world)
    cycle()
    pass_id = ag.only_pass()
    unity.identities.append(_person("z", first="Zed", email="zed@example.com"))
    unity.tokens["z"] = [_token("tz", internal="")]
    cycle()
    records = [e for e in written.all_tokens() if e["person_id"] == "a"]
    _identity_deleted(unity)
    delete = ag.delete

    def fail(card_id):
        raise AccessGridError("temporary outage")

    monkeypatch.setattr(ag, "delete", fail)
    cycle()
    assert [e for e in written.all_tokens() if e["person_id"] == "a"] == records
    assert _row("a").ag_card_id == pass_id
    monkeypatch.setattr(ag, "delete", delete)
    cycle()
    assert [e for e in written.all_tokens() if e["person_id"] == "a"] == []
    assert _row("a").status == "deleted"


@PAIRS
def test_desfire_37_bit_edits_send_encoded_payload(world, pair):
    from agsync.lib.pacs.avigilon import wiegand

    unity = FakeUnity([_person("a")], tokens={"a": [_token("t1", internal="5001")]})
    ag, cycle = world("desfire", pair, unity, card_format="2", facility_code="239",
                      card_format_layout=wiegand.STANDARDS["H10304"].as_params())
    cycle()
    issued = next(p for verb, _, p in ag.calls if verb == "provision")
    assert issued["file_data"] == wiegand.encode(wiegand.STANDARDS["H10304"], "239", "5001")
    unity.tokens["a"][0]["attributes"]["plasecInternalnumber"] = "5002"
    cycle()
    updates = [(cid, p) for verb, cid, p in ag.calls if verb == "update"]
    assert updates == [(ag.only_pass(), {
        "metadata": {**issued["metadata"], "card_number": "5002"},
        "file_data": wiegand.encode(wiegand.STANDARDS["H10304"], "239", "5002"),
    })]


def test_unity_tracking_rejects_duplicate_site_and_number():
    row = written.add("a", "t1", "239", "9001", None, None)
    assert written.add("a", "t1", "239", "9001", None, None) == row
    with pytest.raises(ValueError, match="already tracked"):
        written.add("b", "t2", "239", "9001", None, None)
    assert len(written.all_tokens()) == 1
    written.add("b", "t2", "240", "9001", None, None)
    assert len(written.all_tokens()) == 2


def test_two_non_seos_edits_cannot_claim_the_same_credential_in_one_cycle(world, mailed):
    unity, ag, cycle = _desfire(world, True)
    unity.identities.append(_person("z", first="Zed", email="zed@example.com"))
    unity.tokens["z"] = [_token("tz", internal="5009")]
    cycle()
    for pid in ("a", "z"):
        unity.tokens[pid][0]["attributes"]["plasecInternalnumber"] = "6000"
    cycle()
    assert len([v for v, _, _ in ag.calls if v == "update"]) == 1
    assert any(key.startswith("duplicate:") for key, _ in mailed)


def test_ag_resume_after_unity_suspension_reactivates_tokens(world):
    unity, ag, cycle, pass_id, _ = _installed_seos(world)
    _status(unity.tokens["a"][0], "Inactive")
    cycle()
    ag.set_state(pass_id, "active")
    cycle()
    assert set(_tokens_by_number(unity).values()) == {"Active"}
    assert ag.verbs("suspend", "resume") == [("suspend", pass_id)]


def test_failed_suspend_does_not_turn_the_unity_token_back_on(world, monkeypatch):
    from agsync.ag import AccessGridError

    unity, ag, cycle, pass_id, _ = _installed_seos(world)
    _status(unity.tokens["a"][0], "Inactive")

    def fail(card_id):
        assert card_id == pass_id
        raise AccessGridError("temporary outage")

    monkeypatch.setattr(ag, "suspend", fail)
    cycle()
    cycle()
    assert set(_tokens_by_number(unity).values()) == {"Inactive"}
    assert _row().last_known_ag_state == "active", "failed suspend remains pending"


def test_revoke_of_already_absent_ag_pass_releases_writeback_records(world, monkeypatch):
    from agsync.ag import AccessGridError

    unity, ag, cycle = _seos(world)
    cycle()
    unity.identities.append(_person("z", first="Zed", email="zed@example.com"))
    unity.tokens["z"] = [_token("tz", internal="")]
    cycle()
    _identity_deleted(unity)

    def gone(card_id):
        raise AccessGridError("404 not found")

    monkeypatch.setattr(ag, "delete", gone)
    cycle()
    assert not [e for e in written.all_tokens() if e["person_id"] == "a"]
    assert not [t for t in tracking.all_tracked() if t.pacs_person_id == "a"]
