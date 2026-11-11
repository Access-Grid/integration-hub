"""Avigilon Unity: reads that fail raise, writes that can't echo refuse.

Runs the real client against a fake Unity behind httpx.MockTransport, so
the session handling, paging and form building are all exercised — only
the appliance is pretend. Response shapes follow what the avigilon-service
agent captured from a live server.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest

from agsync.lib.pacs.avigilon import written
from agsync.lib.pacs.avigilon.adapter import AvigilonAdapter, parse_date
from agsync.lib.pacs.avigilon.client import (
    ROSTER_TIMEOUT,
    TOKEN_FORM_DATES,
    TOKEN_FORM_FIELDS,
    XML_PAGE_SIZE,
    UnityAuthError,
    UnityClient,
    UnityError,
)
from agsync.lib.pacs.base import CredentialIdentity, CredentialStatus, PacsRecordUnavailable
from agsync.sync import engine as engine_mod
from agsync.sync import snapshot as snapshot_mod
from agsync.sync import tracking
from agsync.sync.engine import SyncEngine
from agsync.sync.snapshot import Snapshot, build_snapshot


def _identity(cn, name="Cooper, Jim", status="Active"):
    return {"cn": cn, "plasecName": name, "plasecIdstatus": status}


def _token(cn, internal="42069", embossed="AccessGrid", status="Active", deactivate="", **extra):
    attrs = {f: "FALSE" for f in TOKEN_FORM_FIELDS} | {f: "" for f in TOKEN_FORM_DATES}
    attrs |= {"cn": cn, "plasecInternalnumber": internal, "plasecEmbossednumber": embossed,
              "plasecTokennoexpire": "TRUE", **extra,
              "extended_attributes": {"token_status": status, "formatted_deactivate_date": deactivate}}
    return {"id": cn, "type": "Token", "attributes": attrs}


class FakeUnity:
    """Just enough of Unity to sign in, page identities and edit tokens."""

    def __init__(self, identities=(), tokens=None, details=None, per_page_cap=None):
        self.identities = list(identities)
        self.tokens = tokens or {}
        self.details = details or {}
        self.per_page_cap = per_page_cap  # serve every page as page 1 when set
        self.accept = "form"
        self.signed_in = False
        self.fail = {}  # path -> status code
        self.posts = []
        self.ignore_writes = False
        self.default_on_write = {}  # fields Unity sets on a write we did not send
        self.json_off = False  # a box with only the XML endpoints
        self.gets = []
        self.raw = {}  # path -> body served verbatim once signed in
        # Where the CSRF token is handed out: "meta" (the live box), "cookie", or None.
        self.csrf_where = "meta"
        self.csrf = "csrf-abc"
        self.xml_params = []  # query params of each /identities.xml read
        self.timeouts = {}  # path -> read timeout of the last request for it
        self.xml_paging = "honour"  # or "ignore" (send everything) or "repeat" (always page 1)
        self.search_fails = False
        self.hide_once = set()  # left out of the next roster read only: a row skipped mid-read
        self.detail_404 = False  # a box answering 404 for every identity's detail

    @staticmethod
    def _xml(root, row, rows):
        body = "".join(f"<{row}>" + "".join(f"<{k}>{v}</{k}>" for k, v in r.items()) + f"</{row}>" for r in rows)
        return httpx.Response(200, text=f"<{root}>{body}</{root}>", headers={"content-type": "application/xml"})

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET":
            self.gets.append(path)
        self.timeouts[path] = req.extensions.get("timeout", {}).get("read")
        if path in self.fail:
            return httpx.Response(self.fail[path])
        if path in self.raw and self.signed_in:
            return httpx.Response(200, text=self.raw[path])
        if self.json_off and path.endswith(".json"):
            return httpx.Response(404)
        if path == "/sessions" and req.method == "POST":
            kind = "json" if "json" in req.headers.get("content-type", "") else "form"
            self.signed_in = kind == self.accept
            headers = {"Location": "/"}
            if self.signed_in and self.csrf_where == "cookie":
                headers["set-cookie"] = f"XSRF-TOKEN={self.csrf}; path=/"
            return httpx.Response(302 if kind == "form" else 200, headers=headers)
        if path == "/sessions/new":
            return httpx.Response(200, text="<html>sign in</html>")
        if not self.signed_in:
            return httpx.Response(302, headers={"Location": "https://unity/sessions/new"})
        if path == "/":
            meta = f'<meta name="csrf-token" content="{self.csrf}" />' if self.csrf_where == "meta" else ""
            return httpx.Response(200, text=f"<html><head>{meta}</head>dashboard</html>")
        if path == "/identities.json":
            page, per = int(req.url.params.get("page", 1)), int(req.url.params.get("perpage", 100))
            start = 0 if self.per_page_cap else (page - 1) * per
            rows = self.identities[start:start + per]
            return httpx.Response(200, json={"data": rows, "meta": {"recordsFiltered": len(self.identities)}})
        parts = path.strip("/").split("/")
        if path == "/identities.xml":
            q = dict(req.url.params)
            self.xml_params.append(q)
            rows = self.identities
            if q.get("adv_search_field_0") == "plasecEmbossednumber":
                if self.search_fails:
                    return httpx.Response(500)
                # Like the live search: any case, and only active tokens.
                rows = [r for r in rows if any(
                    t["attributes"]["plasecEmbossednumber"].lower() == q["adv_search_val_0"].lower()
                    and t["attributes"]["extended_attributes"]["token_status"] == "Active"
                    for t in self.tokens.get(r["cn"], []))]
            elif q.get("perpage") != "1" and self.hide_once:
                rows, self.hide_once = [r for r in rows if r["cn"] not in self.hide_once], set()
            if "page" in q and self.xml_paging != "ignore":  # the live box honours page/perpage
                page, per = int(q["page"]) if self.xml_paging == "honour" else 1, int(q["perpage"])
                rows = rows[(page - 1) * per:page * per]
            return self._xml("identities", "identity", rows)
        if parts[-1] == "tokens.xml":
            rows = [{k: v for k, v in t["attributes"].items() if k != "extended_attributes"}
                    | {"plasecTokenstatus": {"Active": "1", "Inactive": "2"}[
                        t["attributes"]["extended_attributes"]["token_status"]]}
                    for t in self.tokens.get(parts[1], [])]
            return self._xml("tokens", "token", rows)
        if parts[-1] == "tokens.json":
            return httpx.Response(200, json={"tokens": self.tokens.get(parts[1], []), "recordsTotal": 0})
        if path.endswith(".json") and len(parts) == 2:
            cn = parts[1][:-5]
            listed = next((r for r in self.identities if r["cn"] == cn), None)
            if self.detail_404 or (cn not in self.details and listed is None):
                return httpx.Response(404)
            return httpx.Response(200, json={"data": self.details.get(cn, listed)})
        if parts[-1] == "new" and req.method == "GET":
            return httpx.Response(200, text=(
                f'<form action="/identities/{parts[1]}/tokens" method="post">'
                '<input name="utf8" type="hidden" value="✓"><input name="authenticity_token" value="x">'
                '<input name="token[plasecInternalnumber]"><input name="token[plasecEmbossednumber]">'
                '<select name="token[plasecTokenstatus]"><option value="1">Active</option></select></form>'))
        if req.method == "POST" and len(parts) in (3, 4) and parts[2] == "tokens":
            form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
            if self.csrf_where is None or form.get("authenticity_token") != self.csrf \
                    or req.headers.get("X-CSRF-Token") != self.csrf:
                return httpx.Response(422, text="InvalidAuthenticityToken")
            self.posts.append(form)
            tokens = self.tokens.setdefault(parts[1], [])
            if len(parts) == 3:  # create
                made = _token(f"new{len(self.posts)}", internal=form["token[plasecInternalnumber]"],
                              embossed=form["token[plasecEmbossednumber]"],
                              status={"1": "Active", "2": "Inactive"}[form["token[plasecTokenstatus]"]])
                if not self.ignore_writes:
                    tokens.append(made)
                return httpx.Response(302, headers={"Location": f"/identities/{parts[1]}/tokens/{made['id']}"})
            if form.get("_method") == "delete":
                if not self.ignore_writes:
                    tokens[:] = [t for t in tokens if t["id"] != parts[3]]
                return httpx.Response(302, headers={"Location": f"/identities/{parts[1]}"})
            for t in [] if self.ignore_writes else self.tokens[parts[1]]:
                if t["id"] == parts[3]:
                    t["attributes"].update(self.default_on_write)
                    t["attributes"]["extended_attributes"]["token_status"] = {"1": "Active", "2": "Inactive"}[
                        form["token[plasecTokenstatus]"]]
            return httpx.Response(302, headers={"Location": f"/identities/{parts[1]}"})
        return httpx.Response(404)


def _client(fake) -> UnityClient:
    c = UnityClient("unity", "admin", "pw")
    c._client = httpx.Client(base_url="https://unity", transport=httpx.MockTransport(fake))
    return c


def _adapter(fake) -> AvigilonAdapter:
    a = AvigilonAdapter("unity", "admin", "pw")
    a._client = _client(fake)
    return a


# --- session ---------------------------------------------------------------


def test_form_login_is_accepted():
    _client(FakeUnity()).login()


def test_json_login_is_the_fallback():
    fake = FakeUnity()
    fake.accept = "json"
    _client(fake).login()


def test_rejected_login_raises():
    fake = FakeUnity()
    fake.accept = "neither"
    with pytest.raises(UnityAuthError):
        _client(fake).login()


def test_expired_session_is_renewed_once():
    fake = FakeUnity(identities=[_identity("a")])
    c = _client(fake)
    c.login()
    fake.signed_in = False  # the appliance drops the session
    assert [r["id"] for r in c.list_identities()] == ["a"]


# --- JSON first, XML fallback ---------------------------------------------------


def test_xml_only_box_signs_in_and_reads():
    fake = FakeUnity([_identity("a")], tokens={"a": [_token("t1")]},
                     details={"a": {"id": "a", "attributes": {}}})
    fake.json_off = True
    a = _adapter(fake)
    assert [p.id for p in a.list_people() if p.id == "a"] == ["a"]
    (cred,) = a.list_credentials("a")
    assert (cred.card_number, cred.trigger_active, cred.status) == ("42069", True, CredentialStatus.ACTIVE)


def test_json_failure_falls_back_to_xml():
    fake = FakeUnity([_identity("a")], tokens={"a": [_token("t1")]})
    c = _client(fake)
    c.login()
    fake.fail["/identities/a/tokens.json"] = 500
    assert [t["id"] for t in c.list_tokens("a")] == ["t1"]


def test_after_falling_back_xml_is_tried_first():
    fake = FakeUnity([_identity("a")], tokens={"a": [_token("t1")]})
    c = _client(fake)
    c.login()
    fake.fail["/identities/a/tokens.json"] = 500
    c.list_tokens("a")
    fake.gets.clear()
    c.list_tokens("a")
    assert fake.gets == ["/identities/a/tokens.xml"]


def test_both_failing_raises_with_both_reasons():
    fake = FakeUnity([_identity("a")])
    c = _client(fake)
    c.login()
    fake.fail["/identities/a/tokens.json"] = 500
    fake.fail["/identities/a/tokens.xml"] = 503
    with pytest.raises(UnityError, match="JSON.*500.*XML.*503"):
        c.list_tokens("a")


def _xml_tokens_answer(body):
    fake = FakeUnity([_identity("a")])
    fake.json_off = True
    fake.raw["/identities/a/tokens.xml"] = body
    return _client(fake).list_tokens("a")


def test_html_instead_of_xml_raises_rather_than_reading_as_no_tokens():
    with pytest.raises(UnityError, match="not XML"):
        _xml_tokens_answer("<html><body>Sign in<br></body></html>")


def test_unexpected_xml_document_raises():
    with pytest.raises(UnityError, match="expected <tokens>"):
        _xml_tokens_answer("<errors><error>boom</error></errors>")


def test_rails_empty_array_xml_is_no_tokens():
    assert _xml_tokens_answer('<nil-classes type="array"/>') == []


# --- roster ------------------------------------------------------------------


def test_every_page_is_read(monkeypatch):
    monkeypatch.setattr("agsync.lib.pacs.avigilon.client.PAGE_SIZE", 2)
    fake = FakeUnity(identities=[_identity(str(i)) for i in range(5)])
    assert len(_client(fake).list_identities()) == 5


def test_ignored_paging_raises_instead_of_returning_page_one(monkeypatch):
    monkeypatch.setattr("agsync.lib.pacs.avigilon.client.PAGE_SIZE", 2)
    fake = FakeUnity(identities=[_identity(str(i)) for i in range(5)], per_page_cap=True)
    c = _client(fake)
    with pytest.raises(UnityError, match="paging ignored"):
        c._identities_json()
    # …and the whole roster then comes from XML, which is not paged.
    assert len(c.list_identities()) == 5


def test_outage_while_signing_in_is_an_error_not_a_wrong_password():
    fake = FakeUnity(identities=[_identity("a")])
    fake.fail["/identities.json"] = 500
    fake.fail["/identities.xml"] = 500
    with pytest.raises(UnityError, match="would not answer") as raised:
        list(_adapter(fake).list_people())
    assert not isinstance(raised.value, UnityAuthError)


# --- tokens and credentials ----------------------------------------------------


def _read(fake, pid="a"):
    a = _adapter(fake)
    people = {p.id: p for p in a.list_people()}
    return a, people, list(a.list_credentials(pid))


def test_trigger_token_maps_to_credential():
    fake = FakeUnity([_identity("a")], tokens={"a": [_token("t1")]},
                     details={"a": {"id": "a", "attributes": {"plasecFname": "Jim", "plasecLname": "Cooper",
                                                             "plasecidentityEmailaddress": "jim@x.com"}}})
    _, people, (cred,) = _read(fake)
    assert people["a"].email == "jim@x.com"
    assert (cred.card_number, cred.trigger_active, cred.hold) == ("42069", True, False)
    assert cred.status is CredentialStatus.ACTIVE


def test_trigger_without_internal_number_is_held_not_numbered_accessgrid():
    fake = FakeUnity([_identity("a")], tokens={"a": [_token("t1", internal="")]},
                     details={"a": {"id": "a", "attributes": {}}})
    _, _, (cred,) = _read(fake)
    assert cred.card_number == "" and cred.hold


def test_failed_token_read_is_unavailable_not_empty():
    fake = FakeUnity([_identity("a")])
    fake.fail["/identities/a/tokens.json"] = 500
    fake.fail["/identities/a/tokens.xml"] = 500
    with pytest.raises(PacsRecordUnavailable):
        _read(fake)


def test_failed_detail_read_holds_the_persons_tokens():
    fake = FakeUnity([_identity("a")], tokens={"a": [_token("t1")]})
    fake.fail["/identities/a.json"] = 500
    _, _, (cred,) = _read(fake)
    assert cred.hold


def test_unreadable_expiry_holds_rather_than_never_expiring():
    fake = FakeUnity([_identity("a")], tokens={"a": [_token("t1", deactivate="next tuesday")]},
                     details={"a": {"id": "a", "attributes": {}}})
    _, _, (cred,) = _read(fake)
    assert cred.hold and cred.deactivate_date is None


def test_unknown_identity_status_counts_as_active():
    fake = FakeUnity([_identity("a", status="Pending review")], tokens={"a": [_token("t1")]},
                     details={"a": {"id": "a", "attributes": {}}})
    _, people, _ = _read(fake)
    assert people["a"].active


@pytest.mark.parametrize("text", ["2026-03-10T00:00:00Z", "20260310000000Z", "03/10/2026 12:00 AM", "03/10/2026"])
def test_dates_parse_to_aware_datetimes(text):
    parsed = parse_date(text)
    assert parsed.tzinfo is not None
    assert parsed.astimezone(UTC).date() in {datetime(2026, 3, 9).date(), datetime(2026, 3, 10).date()}


# --- writes ----------------------------------------------------------------------


def test_suspend_echoes_every_field_unchanged():
    fake = FakeUnity([_identity("a")], tokens={"a": [_token("t1")]})
    assert _adapter(fake).update_credential_status("a", "t1", CredentialStatus.SUSPENDED)
    (form,) = fake.posts
    assert form["token[plasecTokenstatus]"] == "2"
    assert form["token[plasecTokennoexpire]"] == "TRUE"  # not reset to a default
    assert all(f"token[{f}]" in form for f in TOKEN_FORM_FIELDS)


def test_field_unity_did_not_return_is_left_out_not_invented():
    # The live box omits every field that was never set.
    token = _token("t1")
    del token["attributes"]["plasecTokenVIP"]
    fake = FakeUnity([_identity("a")], tokens={"a": [token]})
    assert _adapter(fake).update_credential_status("a", "t1", CredentialStatus.SUSPENDED)
    (form,) = fake.posts
    assert "token[plasecTokenVIP]" not in form
    assert form["token[plasecTokennoexpire]"] == "TRUE"


def test_write_that_also_changes_another_field_is_a_failure():
    token = _token("t1")
    del token["attributes"]["plasecTokenVIP"]
    fake = FakeUnity([_identity("a")], tokens={"a": [token]})
    fake.default_on_write = {"plasecTokenVIP": "TRUE"}  # Unity filling in a field we left out
    assert not _adapter(fake).update_credential_status("a", "t1", CredentialStatus.SUSPENDED)


def test_write_that_does_not_stick_is_a_failure():
    fake = FakeUnity([_identity("a")], tokens={"a": [_token("t1")]})
    fake.ignore_writes = True  # answers 302, changes nothing
    assert not _adapter(fake).update_credential_status("a", "t1", CredentialStatus.SUSPENDED)


def test_csrf_token_comes_from_the_dashboard_meta_tag():
    fake = FakeUnity([_identity("a")], tokens={"a": [_token("t1")]})  # meta tag, no cookie: the live box
    assert _adapter(fake).update_credential_status("a", "t1", CredentialStatus.SUSPENDED)
    assert fake.posts[0]["authenticity_token"] == "csrf-abc"


def test_csrf_cookie_is_the_fallback():
    fake = FakeUnity([_identity("a")], tokens={"a": [_token("t1")]})
    fake.csrf_where = "cookie"
    assert _adapter(fake).update_credential_status("a", "t1", CredentialStatus.SUSPENDED)


def test_no_csrf_token_refuses_the_write_without_sending_it():
    fake = FakeUnity([_identity("a")], tokens={"a": [_token("t1")]})
    fake.csrf_where = None
    assert not _adapter(fake).update_credential_status("a", "t1", CredentialStatus.SUSPENDED)
    assert not [g for g in fake.gets if "/tokens/" in g] and fake.posts == []


def test_ldap_dates_are_utc():
    # As the live box sends them; the Z once read as local time, hours off.
    assert parse_date("20260520164208Z") == datetime(2026, 5, 20, 16, 42, 8, tzinfo=UTC)


def test_sign_in_check_asks_xml_for_one_row():
    # The whole XML list took 78s on the live box, past the 30s limit.
    fake = FakeUnity([_identity(c) for c in "abc"])
    fake.json_off = True
    _client(fake).login()
    assert fake.xml_params[0]["page"] == "1" and fake.xml_params[0]["perpage"] == "1"


def test_whole_roster_read_gets_the_long_timeout():
    fake = FakeUnity([_identity("a")])
    fake.json_off = True
    client = _client(fake)
    client.list_identities()
    assert fake.timeouts["/identities.xml"] == ROSTER_TIMEOUT
    assert fake.xml_params[-1]["perpage"] == str(XML_PAGE_SIZE)


def test_xml_roster_reads_every_page(monkeypatch):
    monkeypatch.setattr("agsync.lib.pacs.avigilon.client.XML_PAGE_SIZE", 2)
    fake = FakeUnity([_identity(str(i)) for i in range(5)])
    fake.json_off = True
    assert len(_client(fake).list_identities()) == 5
    assert [q["page"] for q in fake.xml_params[1:]] == ["1", "2", "3"]  # after the sign-in check


def test_xml_box_ignoring_paging_sends_the_whole_list(monkeypatch):
    monkeypatch.setattr("agsync.lib.pacs.avigilon.client.XML_PAGE_SIZE", 2)
    fake = FakeUnity([_identity(str(i)) for i in range(5)])
    fake.json_off, fake.xml_paging = True, "ignore"
    assert len(_client(fake).list_identities()) == 5


def test_xml_page_repeating_earlier_pages_raises(monkeypatch):
    monkeypatch.setattr("agsync.lib.pacs.avigilon.client.XML_PAGE_SIZE", 2)
    fake = FakeUnity([_identity(str(i)) for i in range(5)])
    fake.json_off, fake.xml_paging = True, "repeat"
    with pytest.raises(UnityError, match="paging ignored"):
        _client(fake).list_identities()


def _xml_identity(cn, **fields):
    return {"cn": cn, "plasecFname": "Jim", "plasecLname": "Cooper", "plasecIdstatus": "1", **fields}


def test_list_fields_are_not_blanked_by_a_sparser_detail():
    fake = FakeUnity([_xml_identity("a", plasecidentityTitle="Nurse")], tokens={"a": [_token("t1")]},
                     details={"a": {"cn": "a", "plasecFname": "Jim", "plasecidentityEmailaddress": "j@x.co"}})
    (person,) = _adapter(fake).list_people()
    assert person.title == "Nurse" and person.email == "j@x.co"


def test_detail_is_not_fetched_when_the_list_has_the_email():
    fake = FakeUnity([_xml_identity("a", plasecidentityEmailaddress="j@x.co")], tokens={"a": [_token("t1")]})
    (person,) = _adapter(fake).list_people()
    assert person.email == "j@x.co" and "/identities/a.json" not in fake.gets


# --- in the engine --------------------------------------------------------------


@pytest.fixture
def tracked(monkeypatch):
    """Make the tracking table hold a pass for each person id given."""
    def hold(*pids):
        rows = [SimpleNamespace(pacs_person_id=p, ag_card_id=f"card-{p}", status="active",
                                last_synced_full_name=f"Tracked {p}", last_synced_email=f"{p}@x",
                                last_synced_phone="", last_synced_title="") for p in pids]
        monkeypatch.setattr(tracking, "all_tracked", lambda: rows)
    monkeypatch.setattr(snapshot_mod, "list_cards", lambda *a: [])
    return hold


def test_deactivated_identity_holding_a_pass_reads_as_suspended_not_gone(tracked):
    tracked("b")
    fake = FakeUnity([_identity("a"), _identity("b", status="Inactive"), _identity("c", status="Inactive")],
                     tokens={"a": [_token("ta", embossed="")], "b": [_token("tb")], "c": [_token("tc")]})
    snap = build_snapshot(_adapter(fake), None, "t")
    (cred,) = snap.credentials_by_person["b"]
    assert cred.status == CredentialStatus.SUSPENDED and cred.trigger_active
    assert snap.credentials_by_person["c"] == []  # no pass: not read at all
    assert not any("/identities/c/" in g for g in fake.gets)


def test_unreadable_tracking_raises_rather_than_reading_only_the_search(monkeypatch):
    # Reading only the search would take every pass outside it for deleted.
    def broken():
        raise RuntimeError("db locked")
    monkeypatch.setattr(tracking, "all_tracked", broken)
    with pytest.raises(UnityError, match="tracking table"):
        list(_adapter(_search_fake()).list_people())


def test_engine_runs_an_adapter_that_writes_no_credentials(monkeypatch):
    # The engine once read supports_credential_writeback directly, which only
    # Millennium defines: every Unity cycle failed before reading anything.
    monkeypatch.setattr(engine_mod.AccessGridConfig, "load",
                        lambda: {"account_id": "a", "api_secret": "s", "template_id": "t"})
    monkeypatch.setattr(engine_mod.PacsConfig, "load", lambda: {"vendor": "avigilon", "params": {}, "options": {}})
    monkeypatch.setattr(engine_mod, "build_ag_client", lambda *a: object())
    monkeypatch.setattr(engine_mod, "build_snapshot", lambda *a: Snapshot())
    for name in ("phase1_provision", "phase2_local_to_ag", "phase3_deletions",
                 "phase4_ag_to_local", "phase5_retries", "phase6_field_changes"):
        monkeypatch.setattr(getattr(engine_mod, name), "run", lambda *a, **k: 0)
    engine = SyncEngine()
    monkeypatch.setattr(engine, "_with_derived_mode", lambda ag, a, p: p)
    monkeypatch.setattr(engine, "_pacs_adapter", lambda cfg: AvigilonAdapter("unity", "admin", "pw"))
    assert engine._run_one_cycle_locked().error is None


def test_trigger_holders_without_email_or_phone_are_counted(tracked, monkeypatch):
    tracked()
    monkeypatch.setattr(tracking, "get", lambda pid, cid: None)
    fake = FakeUnity([_identity("a"), _xml_identity("b", plasecidentityEmailaddress="b@x.co")],
                     tokens={"a": [_token("ta")], "b": [_token("tb")]})
    adapter = _adapter(fake)
    adapter._client.get_identity = lambda pid: {}  # the detail adds nothing either
    assert engine_mod._no_contact(build_snapshot(adapter, None, "t")) == 1


# --- embossed-number search -------------------------------------------------------


def _search_fake():
    """a holds a trigger; b's trigger is inactive, which the search skips; c has none."""
    return FakeUnity(
        [_identity("a"), _identity("b"), _identity("c")],
        tokens={"a": [_token("ta")], "b": [_token("tb", status="Inactive")], "c": [_token("tc", embossed="9")]},
        details={p: {"id": p, "attributes": {"plasecidentityEmailaddress": f"{p}@x"}} for p in "abc"},
    )


def test_tokens_are_read_for_search_hits_and_tracked_people_only(tracked):
    tracked("b")
    fake = _search_fake()
    a = _adapter(fake)
    assert {p.id for p in a.list_people()} == {"a", "b"}
    assert not any("/c/" in g for g in fake.gets)
    [ta] = a.list_credentials("a")
    [tb] = a.list_credentials("b")  # missed by the search, read because tracked
    assert ta.trigger_active and tb.trigger_active and tb.status is CredentialStatus.SUSPENDED
    with pytest.raises(PacsRecordUnavailable):
        a.list_credentials("c")


def test_failed_search_reads_tracked_people_only(tracked):
    tracked("b")
    fake = _search_fake()
    fake.search_fails = True
    a = _adapter(fake)
    assert {p.id for p in a.list_people()} == {"b"}
    assert [c.id for c in a.list_credentials("b")] == ["tb"]


def test_roster_is_never_read_whole(tracked):
    tracked("b")
    fake = _search_fake()
    list(_adapter(fake).list_people())
    # Besides the search, only sign-in's one-row check touches the list.
    assert all(q.get("adv_search_field_0") == "plasecEmbossednumber" or q["perpage"] == "1"
               for q in fake.xml_params)
    assert fake.gets.count("/identities.json") <= 1


def test_tracked_identity_unity_404s_is_gone(tracked):
    tracked("gone")
    a = _adapter(_search_fake())
    assert "gone" not in {p.id for p in a.list_people()}


def test_404_with_no_detail_read_working_is_unread_not_gone(tracked):
    tracked("b")
    fake = _search_fake()
    fake.detail_404 = True
    a = _adapter(fake)
    people = {p.id: p for p in a.list_people()}
    assert people["b"].full_name == "Tracked b"
    with pytest.raises(PacsRecordUnavailable):
        a.list_credentials("b")


def test_tracked_identity_whose_detail_fails_stands_in_unread(tracked):
    tracked("b")
    fake = _search_fake()
    fake.fail["/identities/b.json"] = 500
    a = _adapter(fake)
    people = {p.id: p for p in a.list_people()}
    assert (people["b"].full_name, people["b"].email) == ("Tracked b", "b@x")
    assert not any("/b/tokens" in g for g in fake.gets)
    with pytest.raises(PacsRecordUnavailable):
        a.list_credentials("b")


def test_phase3_judges_a_tracked_only_read_by_tracked_people(tracked):
    from agsync.sync.phases import phase3_deletions

    tracked("a", "b", "c", "d")
    snap = Snapshot()
    snap.people = {"a": object()}  # 1 of 4 back: under 30%
    assert not phase3_deletions._tracked_complete(snap)
    snap.people["b"] = object()
    assert phase3_deletions._tracked_complete(snap)


# --- creating and deleting tokens (Seos) -------------------------------------------


def test_created_token_is_read_back_and_its_id_returned():
    fake = FakeUnity([_identity("a")], tokens={"a": []})
    c = _client(fake)
    token_id = c.create_token("a", "777", "accessgrid", "2")
    [made] = c.list_tokens("a")
    assert (made["id"], made["internal_number"], made["embossed_number"], made["status"]) == \
        (token_id, "777", "accessgrid", "2")
    assert "token[plasecTokenstatus]" in fake.posts[-1] and "_method" not in fake.posts[-1]


def test_create_that_does_not_stick_raises():
    fake = FakeUnity([_identity("a")], tokens={"a": []})
    fake.ignore_writes = True
    with pytest.raises(UnityError, match="0 tokens read back"):
        _client(fake).create_token("a", "777", "accessgrid", "2")


def test_create_without_csrf_is_refused_unsent():
    fake = FakeUnity([_identity("a")], tokens={"a": []})
    fake.csrf_where = None
    with pytest.raises(UnityError, match="CSRF"):
        _client(fake).create_token("a", "777", "accessgrid", "2")
    assert fake.posts == []


def test_deleted_token_is_gone_on_re_read():
    fake = FakeUnity([_identity("a")], tokens={"a": [_token("t1"), _token("t2")]})
    c = _client(fake)
    c.delete_token("a", "t1")
    assert [t["id"] for t in c.list_tokens("a")] == ["t2"]


def test_delete_that_does_not_stick_raises():
    fake = FakeUnity([_identity("a")], tokens={"a": [_token("t1")]})
    fake.ignore_writes = True
    with pytest.raises(UnityError, match="still on"):
        _client(fake).delete_token("a", "t1")


# --- Seos: AccessGrid allocates, the hub writes tokens -------------------------------


def _seos(tokens=None):
    """a holds enrollment token t1; tracked, so their tokens are always read."""
    fake = FakeUnity([_identity("a")], tokens={"a": tokens if tokens is not None else [_token("t1")]},
                     details={"a": {"id": "a", "attributes": {}}})
    a = _adapter(fake)
    a.seos = True
    return fake, a


def _cycle(a, pid="a"):
    list(a.list_people())
    return list(a.list_credentials(pid))


def _card(number, site="239"):
    return CredentialIdentity(site_code=site, card_number=number)


@pytest.fixture
def seos(tracked):
    tracked("a")
    return _seos


def test_seos_enrollment_asks_accessgrid_for_the_number(seos):
    _, a = seos()
    [cred] = _cycle(a)
    assert (cred.id, cred.card_number, cred.allocate_identity) == ("t1", "", True)
    assert cred.status is CredentialStatus.ACTIVE and not cred.hold


def test_seos_writeback_creates_an_inactive_token_and_records_it(seos):
    fake, a = seos()
    _cycle(a)
    assert a.write_back_credentials("a", "t1", [_card("9001")])
    made = next(t for t in fake.tokens["a"] if t["id"] != "t1")["attributes"]
    assert (made["plasecInternalnumber"], made["plasecEmbossednumber"],
            made["extended_attributes"]["token_status"]) == ("9001", "accessgrid", "Inactive")
    assert a.written_credentials() == {("a", "t1"): ["9001"]}


def test_seos_hub_token_is_not_read_as_an_enrollment(seos):
    _, a = seos()
    _cycle(a)
    a.write_back_credentials("a", "t1", [_card("9001")])
    [cred] = _cycle(a)
    assert cred.id == "t1" and cred.status is CredentialStatus.AWAITING_INSTALL


def test_seos_writeback_offered_again_creates_nothing(seos):
    fake, a = seos()
    _cycle(a)
    a.write_back_credentials("a", "t1", [_card("9001")])
    posts = len(fake.posts)
    assert a.write_back_credentials("a", "t1", [_card("9001")])
    assert len(fake.posts) == posts


def test_seos_unrecorded_token_with_the_number_is_adopted_not_duplicated(seos):
    fake, a = seos([_token("t1"), _token("lost", internal="9001", status="Inactive")])
    _cycle(a)
    assert a.write_back_credentials("a", "t1", [_card("9001")])
    assert fake.posts == []
    assert [e["token_id"] for e in written.for_credential("a", "t1")] == ["lost"]


def test_seos_number_written_for_another_pass_is_refused(seos):
    fake, a = seos()
    written.add("b", "tb", "elsewhere", "239", "9001")
    _cycle(a)
    assert not a.write_back_credentials("a", "t1", [_card("9001")])
    assert fake.posts == []


def test_seos_install_switches_tokens_on_and_suspend_off(seos):
    fake, a = seos()
    _cycle(a)
    a.write_back_credentials("a", "t1", [_card("9001")])
    assert a.update_credential_status("a", "t1", CredentialStatus.ACTIVE)
    [cred] = _cycle(a)
    assert cred.status is CredentialStatus.ACTIVE
    assert a.update_credential_status("a", "t1", CredentialStatus.SUSPENDED)
    made = next(t for t in fake.tokens["a"] if t["id"] != "t1")
    assert made["attributes"]["extended_attributes"]["token_status"] == "Inactive"
    assert fake.tokens["a"][0]["attributes"]["extended_attributes"]["token_status"] == "Active"


def test_seos_token_switched_off_by_an_operator_suspends_and_stays_off(seos):
    fake, a = seos()
    _cycle(a)
    a.write_back_credentials("a", "t1", [_card("9001")])
    a.update_credential_status("a", "t1", CredentialStatus.ACTIVE)
    made = next(t for t in fake.tokens["a"] if t["id"] != "t1")
    made["attributes"]["extended_attributes"]["token_status"] = "Inactive"  # operator
    [cred] = _cycle(a)
    assert cred.status is CredentialStatus.SUSPENDED
    posts = len(fake.posts)
    assert not a.update_credential_status("a", "t1", CredentialStatus.ACTIVE)
    assert len(fake.posts) == posts


def test_seos_deleted_hub_token_suspends_the_pass(seos):
    fake, a = seos()
    _cycle(a)
    a.write_back_credentials("a", "t1", [_card("9001")])
    fake.tokens["a"] = [t for t in fake.tokens["a"] if t["id"] == "t1"]
    [cred] = _cycle(a)
    assert cred.status is CredentialStatus.SUSPENDED


def test_seos_retirement_deletes_the_token_and_forgets_it(seos):
    fake, a = seos()
    _cycle(a)
    a.write_back_credentials("a", "t1", [_card("9001"), _card("9002")])
    assert a.retire_credentials("a", "t1", [_card("9001")]) == 1
    assert [t["attributes"]["plasecInternalnumber"] for t in fake.tokens["a"]] == ["42069", "9002"]
    assert a.written_credentials() == {("a", "t1"): ["9002"]}


def test_desfire_template_keeps_reading_internal_numbers(seos):
    _, a = seos()
    a.seos = False
    [cred] = _cycle(a)
    assert (cred.card_number, cred.allocate_identity) == ("42069", False)
    assert not a.supports_credential_writeback and not a.supports_credential_retirement


def test_seos_issue_writes_both_cards_of_a_pair_into_unity_at_once(seos, monkeypatch):
    """Phase 1 against the real adapter: the numbers in the issue response
    reach Unity in the same cycle, one Inactive token per device."""
    from agsync.sync.phases import phase1_provision

    fake, a = seos()
    [cred] = _cycle(a)
    snap = Snapshot()
    snap.people["a"] = SimpleNamespace(id="a", full_name="Jim Cooper", email="j@x.co", phone="",
                                       title="", active=True)
    snap.credentials_by_person["a"] = [cred]
    monkeypatch.setattr(phase1_provision.tracking, "get", lambda *a: None)
    monkeypatch.setattr(phase1_provision.tracking, "upsert", lambda **kw: None)
    sent = []
    issued = SimpleNamespace(id="card-1", state="created", details=[
        SimpleNamespace(device_type="iphone", site_code="239", card_number="9001", state="created"),
        SimpleNamespace(device_type="apple_watch", site_code="239", card_number="9002", state="created"),
    ])
    ag = SimpleNamespace(access_cards=SimpleNamespace(
        provision=lambda **kw: (sent.append(kw), issued)[1], list=lambda **kw: []))

    assert phase1_provision.run(snap, ag, "tpl", pacs=a) == 1
    assert "card_number" not in sent[0]
    made = {t["attributes"]["plasecInternalnumber"]: t["attributes"]["extended_attributes"]["token_status"]
            for t in fake.tokens["a"] if t["id"] != "t1"}
    assert made == {"9001": "Inactive", "9002": "Inactive"}
    assert a.written_credentials() == {("a", "t1"): ["9001", "9002"]}
