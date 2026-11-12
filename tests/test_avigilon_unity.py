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
        self.internal_search_ignored = False  # a box that lists everyone for an Internal Number search
        self.card_formats = []  # served as XML only, as the live box does

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
        if path == "/card_formats.json":
            return httpx.Response(406, json={"status": 406, "error": "Not Acceptable"})
        if path == "/card_formats.xml":
            return self._xml("card-formats", "card-format", self.card_formats)
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
            elif q.get("adv_search_field_0") == "plasecInternalnumber":
                if self.search_fails:
                    return httpx.Response(500)
                if not self.internal_search_ignored:  # loose: a prefix matches too
                    rows = [r for r in rows if any(
                        t["attributes"]["plasecInternalnumber"].startswith(q["adv_search_val_0"])
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
        rows = [SimpleNamespace(pacs_person_id=p, pacs_credential_id=f"t{p}", ag_card_id=f"card-{p}", status="active",
                                last_synced_full_name=f"Tracked {p}", last_synced_email=f"{p}@x",
                                last_synced_phone="", last_synced_title="", last_synced_department="") for p in pids]
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
                     details={"a": {"id": "a", "attributes": {"plasecidentityEmailaddress": "jim@x.test"}}})
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


def test_seos_enrollment_is_deleted_once_its_tokens_are_written(seos):
    fake, a = seos([_token("t1", deactivate="2030-01-31T00:00:00Z")])
    _cycle(a)
    assert a.write_back_credentials("a", "t1", [_card("9001"), _card("9002")])
    assert sorted(t["attributes"]["plasecInternalnumber"] for t in fake.tokens["a"]) == ["9001", "9002"]
    assert fake.posts[-1]["_method"] == "delete"
    [cred] = _cycle(a)  # the pass goes on, under the enrollment's id and dates
    assert (cred.id, cred.allocate_identity) == ("t1", True)
    assert cred.deactivate_date == datetime(2030, 1, 31, tzinfo=UTC)


def test_seos_failed_enrollment_delete_is_retried_next_offer(seos):
    fake, a = seos()
    _cycle(a)
    fake.fail["/identities/a/tokens/t1"] = 500
    assert not a.write_back_credentials("a", "t1", [_card("9001")])
    del fake.fail["/identities/a/tokens/t1"]
    _cycle(a)
    assert a.write_back_credentials("a", "t1", [_card("9001")])
    assert [t["attributes"]["plasecInternalnumber"] for t in fake.tokens["a"]] == ["9001"]


def test_seos_writeback_offered_again_creates_nothing(seos):
    fake, a = seos()
    _cycle(a)
    a.write_back_credentials("a", "t1", [_card("9001")])
    posts = len(fake.posts)
    assert a.write_back_credentials("a", "t1", [_card("9001")])
    assert len(fake.posts) == posts


def test_seos_token_created_but_not_recorded_is_adopted_not_an_enrollment(seos):
    # The crash between creating a token and recording its id: the row
    # written first is what keeps the token from reading as an enrollment.
    fake, a = seos([_token("t1"), _token("lost", internal="9001", status="Inactive")])
    written.add("a", "t1", "239", "9001", None, None)
    [cred] = _cycle(a)
    assert cred.id == "t1"
    assert a.write_back_credentials("a", "t1", [_card("9001")])
    assert [p.get("_method") for p in fake.posts] == ["delete"]  # the enrollment; nothing created
    assert [e["token_id"] for e in written.for_credential("a", "t1")] == ["lost"]


def test_seos_number_written_for_another_pass_is_refused(seos):
    fake, a = seos()
    written.set_token(written.add("b", "tb", "239", "9001", None, None), "elsewhere")
    _cycle(a)
    assert not a.write_back_credentials("a", "t1", [_card("9001")])
    assert fake.posts == []


def _hub_tokens(fake):
    return [t for t in fake.tokens["a"] if t["id"] != "t1"]


def test_seos_install_switches_tokens_on_and_suspend_off(seos):
    fake, a = seos()
    _cycle(a)
    a.write_back_credentials("a", "t1", [_card("9001")])
    assert a.update_credential_status("a", "t1", CredentialStatus.ACTIVE)
    [cred] = _cycle(a)
    assert cred.status is CredentialStatus.ACTIVE
    assert a.update_credential_status("a", "t1", CredentialStatus.SUSPENDED)
    [made] = _hub_tokens(fake)
    assert made["attributes"]["extended_attributes"]["token_status"] == "Inactive"


def test_seos_token_switched_off_by_an_operator_suspends_and_stays_off(seos, mailed):
    fake, a = seos()
    _cycle(a)
    a.write_back_credentials("a", "t1", [_card("9001")])
    a.update_credential_status("a", "t1", CredentialStatus.ACTIVE)
    [made] = _hub_tokens(fake)
    made["attributes"]["extended_attributes"]["token_status"] = "Inactive"  # operator
    [cred] = _cycle(a)
    assert cred.status is CredentialStatus.SUSPENDED
    posts = len(fake.posts)
    assert not a.update_credential_status("a", "t1", CredentialStatus.ACTIVE)
    assert len(fake.posts) == posts
    assert "switched off in Unity after we switched it on" in mailed[0]


def test_seos_one_hub_token_deleted_suspends_the_pass(seos):
    fake, a = seos()
    _cycle(a)
    a.write_back_credentials("a", "t1", [_card("9001"), _card("9002")])
    fake.tokens["a"] = [t for t in fake.tokens["a"] if t["attributes"]["plasecInternalnumber"] != "9001"]
    [cred] = _cycle(a)
    assert cred.status is CredentialStatus.SUSPENDED


def test_seos_every_hub_token_deleted_leaves_no_credential(seos):
    fake, a = seos()
    _cycle(a)
    a.write_back_credentials("a", "t1", [_card("9001")])
    fake.tokens["a"][0]["attributes"]["plasecEmbossednumber"] = "lost card"  # un-embossed counts as gone
    assert _cycle(a) == []


def test_seos_retirement_deletes_the_token_and_forgets_it(seos):
    fake, a = seos()
    _cycle(a)
    a.write_back_credentials("a", "t1", [_card("9001"), _card("9002")])
    assert a.retire_credentials("a", "t1", [_card("9001")]) == 1
    assert [t["attributes"]["plasecInternalnumber"] for t in fake.tokens["a"]] == ["9002"]
    assert a.written_credentials() == {("a", "t1"): ["9002"]}


def _identity_deleted(fake, pid="a"):
    """Unity deletes the identity, and its tokens with it: every read of it 404s."""
    fake.identities = [r for r in fake.identities if r["cn"] != pid]
    fake.details.pop(pid, None)
    fake.tokens.pop(pid, None)
    for path in (f"/identities/{pid}/tokens.json", f"/identities/{pid}/tokens.xml"):
        fake.fail[path] = 404
    for t in ("new1", "new2", "new3"):
        fake.fail[f"/identities/{pid}/tokens/{t}"] = 404


def test_seos_retirement_forgets_the_tokens_of_an_identity_deleted_from_unity(seos):
    fake, a = seos()
    _cycle(a)
    a.write_back_credentials("a", "t1", [_card("9001"), _card("9002")])
    _identity_deleted(fake)
    assert a.retire_credentials("a", "t1", [_card("9001"), _card("9002")]) == 2
    assert a.written_credentials() == {}
    # Forgotten for good: the next offer has nothing left to retry.
    assert a.retire_credentials("a", "t1", [_card("9001"), _card("9002")]) == 0


def test_seos_phase3_stops_retrying_once_the_identity_is_deleted_from_unity(seos, monkeypatch, caplog):
    """The loop this closes: the pass is deleted because its identity left
    Unity, AccessGrid keeps listing it as deleted, and phase 3 tried to
    delete its tokens from the vanished identity every cycle, failing each time."""
    from agsync.sync.phases import phase3_deletions

    fake, a = seos()
    _cycle(a)
    a.write_back_credentials("a", "t1", [_card("9001"), _card("9002")])
    _identity_deleted(fake)
    row = SimpleNamespace(pacs_person_id="a", pacs_credential_id="t1", ag_card_id="pass-a",
                          status="deleted", sync_ref="ref-a")
    monkeypatch.setattr(phase3_deletions.tracking, "all_tracked", lambda: [row])
    snap = Snapshot()
    snap.ag_cards_by_sync_ref["ref-a"] = [SimpleNamespace(
        id="pass-a", state="deleted", details=[
            SimpleNamespace(site_code="239", card_number="9001", state="created"),
            SimpleNamespace(site_code="239", card_number="9002", state="created"),
        ])]

    class Ag:  # the re-read fails, so the listed card stands
        access_cards = SimpleNamespace(get=lambda card_id: (_ for _ in ()).throw(KeyError(card_id)))

    with caplog.at_level("INFO"):
        assert phase3_deletions._retire_deleted_credentials(snap, Ag(), a) == 2
    assert a.written_credentials() == {}
    assert "no longer in Unity" in caplog.text and "Failed to retire" not in caplog.text

    caplog.clear()
    requests = len(fake.gets) + len(fake.posts)
    assert phase3_deletions._retire_deleted_credentials(snap, Ag(), a) == 0
    assert len(fake.gets) + len(fake.posts) == requests, "nothing left to ask Unity about"
    assert "Failed to retire" not in caplog.text


def test_seos_retirement_keeps_the_record_when_unity_cannot_say_the_identity_is_gone(seos):
    fake, a = seos()
    _cycle(a)
    a.write_back_credentials("a", "t1", [_card("9001")])
    for path in ("/identities/a/tokens/new1", "/identities/a/tokens.json", "/identities/a/tokens.xml",
                 "/identities/a.json"):
        fake.fail[path] = 500
    assert a.retire_credentials("a", "t1", [_card("9001")]) == 0
    assert a.written_credentials() == {("a", "t1"): ["9001"]}


def test_seos_retirement_keeps_the_record_when_the_identity_is_there_but_its_tokens_unread(seos):
    fake, a = seos()
    _cycle(a)
    a.write_back_credentials("a", "t1", [_card("9001")])
    for path in ("/identities/a/tokens/new1", "/identities/a/tokens.json", "/identities/a/tokens.xml"):
        fake.fail[path] = 500
    assert a.retire_credentials("a", "t1", [_card("9001")]) == 0
    assert a.written_credentials() == {("a", "t1"): ["9001"]}


def _desfire_tracked_pass(monkeypatch, halves, last_known="active"):
    """a holds DESFire token t1, Active, with a tracked pass whose cards are `halves`.

    Runs phase 4 against the real adapter; returns (fake Unity, recorded AG states).
    """
    from agsync.sync.phases import phase4_ag_to_local

    row = SimpleNamespace(pacs_person_id="a", pacs_credential_id="t1", ag_card_id="pass-a", status="active",
                          sync_ref="ref-a", last_known_ag_state=last_known, full_name="Jim Cooper",
                          last_synced_full_name="Jim Cooper", last_synced_email="a@x",
                          last_synced_phone="", last_synced_title="", last_synced_department="")
    monkeypatch.setattr(tracking, "all_tracked", lambda: [row])
    recorded = []
    monkeypatch.setattr(tracking, "update_last_known_ag_state", lambda p, c, s: recorded.append(s))
    fake, a = _seos()
    a.seos = False
    snap = build_snapshot(a, None, "t")
    snap.ag_cards_by_sync_ref["ref-a"] = [SimpleNamespace(id=f"half-{i}", state=s) for i, s in enumerate(halves)]
    phase4_ag_to_local.run(snap, a, None)
    return fake, recorded


def _t1_status(fake):
    return next(t for t in fake.tokens["a"] if t["id"] == "t1")["attributes"]["extended_attributes"]["token_status"]


@pytest.mark.parametrize("halves", [["unlink"], ["unlinked"], ["deleted", "unlink"], ["unlink", "deleted"]])
def test_desfire_pass_unlinked_in_accessgrid_switches_the_token_off(monkeypatch, halves):
    """Deleted halves are the side of a pair nobody installed: they say nothing."""
    monkeypatch.setattr(snapshot_mod, "list_cards", lambda *a: [])
    fake, recorded = _desfire_tracked_pass(monkeypatch, halves)
    assert _t1_status(fake) == "Inactive"
    assert recorded[-1] in ("unlink", "unlinked")


@pytest.mark.parametrize("halves", [["deleted"], ["deleted", "deleted"]])
def test_desfire_pass_deleted_whole_in_accessgrid_switches_the_token_off(monkeypatch, halves):
    """Every card deleted: the spec's 'suspended, unlinked or deleted'. The
    row is released, so switching the token on again issues a new pass."""
    monkeypatch.setattr(snapshot_mod, "list_cards", lambda *a: [])
    released = []
    monkeypatch.setattr(tracking, "mark_deleted", lambda p, c: released.append((p, c)))
    fake, recorded = _desfire_tracked_pass(monkeypatch, halves)
    assert _t1_status(fake) == "Inactive"
    assert released == [("a", "t1")] and recorded == []


@pytest.mark.parametrize("halves", [["unlink", "active"], ["deleted", "active"]])
def test_desfire_token_stays_on_while_a_half_is_active(monkeypatch, halves):
    """One half still active keeps the token on, whatever the other half says."""
    monkeypatch.setattr(snapshot_mod, "list_cards", lambda *a: [])
    fake, _ = _desfire_tracked_pass(monkeypatch, halves)
    assert _t1_status(fake) == "Active"
    assert not any(p.get("_method") == "put" for p in fake.posts)


def test_desfire_unlinked_pass_is_left_alone_the_cycle_after(monkeypatch):
    """With the token off and the unlink recorded, phase 2 calls nothing on
    AccessGrid (an unlinked pass is not suspended) and phase 4 writes nothing."""
    from agsync.sync.phases import phase2_local_to_ag

    monkeypatch.setattr(snapshot_mod, "list_cards", lambda *a: [])
    fake, _ = _desfire_tracked_pass(monkeypatch, ["deleted", "unlink"])
    assert _t1_status(fake) == "Inactive"
    writes = len(fake.posts)
    # Phase 4 again, the unlink now recorded: nothing new, nothing written.
    fake2, recorded = _desfire_tracked_pass(monkeypatch, ["deleted", "unlink"], last_known="unlink")
    assert recorded == [] and fake2.posts == []

    class NoAg:
        @property
        def access_cards(self):
            raise AssertionError("phase 2 called AccessGrid")

    _, a = _seos(fake.tokens["a"])
    a.seos = False
    snap = build_snapshot(a, None, "t")
    snap.ag_cards_by_sync_ref["ref-a"] = [SimpleNamespace(id="h0", state="deleted"),
                                          SimpleNamespace(id="h1", state="unlink")]
    assert phase2_local_to_ag.run(snap, NoAg()) == 0
    assert len(fake.posts) == writes


@pytest.mark.parametrize(("halves", "state"), [(["deleted", "unlink"], "unlink"), (["unlink", "active"], "active"),
                                               (["deleted", "deleted"], "deleted"), (["suspended", "deleted"], "suspended")])
def test_every_phase_reads_a_pair_by_its_undeleted_half(halves, state):
    """Phases 1, 2 and 4 must agree on the pass's state, or each records
    its own reading over the last and they act on each other's changes."""
    snap = Snapshot()
    snap.ag_cards_by_sync_ref["ref"] = [SimpleNamespace(id=f"h{i}", state=s) for i, s in enumerate(halves)]
    assert snap.resolve_ag_card("pass", sync_ref="ref").state == state


def test_seos_pair_android_half_auto_deleted_on_ios_install_leaves_unity_in_the_same_cycle(seos, monkeypatch):
    """A pair writes one token per platform. The holder installs on an
    iPhone and AccessGrid deletes the Android half: phase 3 deletes its token
    and our record of it, and phase 4 then switches on only the iOS one."""
    from agsync.sync.phases import phase3_deletions, phase4_ag_to_local

    fake, a = seos()
    _cycle(a)
    assert a.write_back_credentials("a", "t1", [_card("9001"), _card("9002")])  # iOS, Android
    row = SimpleNamespace(pacs_person_id="a", pacs_credential_id="t1", ag_card_id="pass-a", status="active",
                          sync_ref="ref-a", last_known_ag_state="active", full_name="Jim Cooper")
    monkeypatch.setattr(tracking, "all_tracked", lambda: [row])
    snap = Snapshot()
    snap.ag_cards_by_sync_ref["ref-a"] = [
        SimpleNamespace(id="ios", state="active", site_code="239", card_number="9001",
                        devices=[SimpleNamespace(device_type="iphone", status="installed")]),
        SimpleNamespace(id="android", state="deleted", site_code="239", card_number="9002", devices=[]),
    ]

    class Ag:  # re-reads fail, so the listed cards stand
        access_cards = SimpleNamespace(get=lambda card_id: (_ for _ in ()).throw(KeyError(card_id)))

    def tokens():
        return {t["attributes"]["plasecInternalnumber"]: t["attributes"]["extended_attributes"]["token_status"]
                for t in fake.tokens["a"]}

    assert phase3_deletions._retire_deleted_credentials(snap, Ag(), a) == 1
    assert tokens() == {"9001": "Inactive"}
    assert a.written_credentials() == {("a", "t1"): ["9001"]}

    snap.credentials_by_person["a"] = _cycle(a)
    assert phase4_ag_to_local._hold_uninstalled_inactive(snap, a, Ag()) == 1
    assert tokens() == {"9001": "Active"}
    [cred] = _cycle(a)
    assert cred.status is CredentialStatus.ACTIVE


def _installed_seos_pass(seos, monkeypatch, re_read):
    """a's Seos pass, installed and its token on; phase 4's install gate run
    with `re_read` answering each card fetch. Returns a's token status after."""
    from agsync.sync.phases import phase4_ag_to_local

    fake, a = seos()
    _cycle(a)
    a.write_back_credentials("a", "t1", [_card("9001")])
    assert a.update_credential_status("a", "t1", CredentialStatus.ACTIVE)
    row = SimpleNamespace(pacs_person_id="a", pacs_credential_id="t1", ag_card_id="pass-a", status="active",
                          sync_ref="ref-a", last_known_ag_state="active", full_name="Jim Cooper")
    monkeypatch.setattr(tracking, "all_tracked", lambda: [row])
    snap = Snapshot()
    # The listing never carries devices: only a card fetch says it is installed.
    snap.ag_cards_by_sync_ref["ref-a"] = [SimpleNamespace(id="ios", state="active", devices=[])]
    snap.credentials_by_person["a"] = _cycle(a)
    phase4_ag_to_local._hold_uninstalled_inactive(snap, a, SimpleNamespace(
        access_cards=SimpleNamespace(get=re_read)))
    [made] = _hub_tokens(fake)
    return made["attributes"]["extended_attributes"]["token_status"]


def test_seos_card_fetch_that_fails_leaves_an_installed_holders_token_on(seos, monkeypatch, caplog):
    """A failed fetch leaves only the listing's devices: [], which is not
    "not installed" but "unknown". Read as not installed, it switched off
    the token of a holder whose pass works, until the next cycle."""
    def fails(card_id):
        raise RuntimeError("429 Too Many Requests")

    with caplog.at_level("WARNING"):
        assert _installed_seos_pass(seos, monkeypatch, fails) == "Active"
    assert "could not be re-read" in caplog.text


def test_seos_card_fetch_saying_nothing_is_installed_still_switches_the_token_off(seos, monkeypatch):
    """The other side of the same gate: a fetch that worked and lists no
    device is a real "not installed", and the token goes off."""
    assert _installed_seos_pass(seos, monkeypatch,
                                lambda card_id: SimpleNamespace(id="ios", state="active", devices=[])) == "Inactive"


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
                                       title="", department="", active=True)
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


def test_unity_issues_up_to_200_a_cycle_others_keep_25():
    from agsync.lib.pacs import get_descriptor
    from agsync.sync.phases import phase1_provision

    assert get_descriptor("avigilon").max_provisions_per_cycle == 200
    assert phase1_provision.MAX_PROVISIONS_PER_CYCLE == 25


def test_unity_waits_at_least_20s_between_cycles_others_60s():
    from agsync.lib.pacs import get_descriptor
    from agsync.sync import engine

    assert get_descriptor("avigilon").min_interval_s == 20
    assert get_descriptor("millennium_ultra").min_interval_s is None
    assert engine.MIN_INTERVAL_S == 60
    assert engine.next_interval(2, 20) == 20
    assert engine.next_interval(10, 20) == 30, "a slower cycle still waits 3x its time"
    assert engine.next_interval(400, 20) == engine.MAX_INTERVAL_S


@pytest.mark.parametrize(("vendor", "floor"), [("avigilon", 20), ("millennium_ultra", 60)])
def test_a_cycle_takes_its_wait_floor_from_the_configured_pacs(monkeypatch, vendor, floor):
    from agsync.sync import engine as engine_mod

    eng = engine_mod.SyncEngine()
    monkeypatch.setattr(engine_mod.AccessGridConfig, "load", lambda: {"account_id": "a", "api_secret": "s",
                                                                       "template_id": "t"})
    monkeypatch.setattr(engine_mod.PacsConfig, "load", lambda: {"vendor": vendor, "params": {}})
    monkeypatch.setattr(engine_mod, "build_ag_client", lambda *a: object())
    monkeypatch.setattr(eng, "_with_derived_mode", lambda ag, ag_cfg, pacs_cfg: pacs_cfg)

    def stop_here(cfg):
        raise RuntimeError("stop after the floor is set")

    monkeypatch.setattr(eng, "_pacs_adapter", stop_here)
    eng._run_one_cycle_locked()
    assert eng._min_interval_s == floor

    monkeypatch.setattr(eng, "_run_one_cycle", lambda: engine_mod.CycleResult(started_at="now", duration_ms=2_000))
    monkeypatch.setattr(eng._trigger, "wait", lambda timeout: eng._stop.set())
    eng._run_loop()
    assert eng.get_status()["cached_interval_s"] == floor


def test_unity_pass_with_no_end_date_lasts_3650_days_others_365(monkeypatch):
    from datetime import timedelta

    from agsync.lib.pacs import get_descriptor
    from agsync.lib.pacs.base import Credential, Person
    from agsync.sync.phases import phase1_provision

    assert get_descriptor("avigilon").default_pass_days == 3650
    monkeypatch.setattr(phase1_provision.tracking, "get", lambda *a: None)
    monkeypatch.setattr(phase1_provision.tracking, "upsert", lambda **kw: None)
    for days in (3650, 365):
        snap = Snapshot()
        snap.people["a"] = Person(id="a", full_name="Jim Cooper", email="j@x.co")
        snap.credentials_by_person["a"] = [Credential(id="t1", person_id="a", card_number="42069",
                                                      status=CredentialStatus.ACTIVE, trigger_active=True)]
        sent = []
        ag = SimpleNamespace(access_cards=SimpleNamespace(
            provision=lambda sent=sent, **kw: (sent.append(kw), SimpleNamespace(id="c", state="created"))[1]))
        kwargs = {"default_pass_days": days} if days != 365 else {}
        phase1_provision.run(snap, ag, "tpl", **kwargs)
        lasts = datetime.fromisoformat(sent[0]["expiration_date"]) - datetime.now(UTC)
        assert timedelta(days=days - 1) < lasts <= timedelta(days=days)


def test_seos_end_date_set_on_a_hub_token_moves_the_pass(seos):
    fake, a = seos([_token("t1", deactivate="2030-01-31T00:00:00Z")])
    _cycle(a)
    a.write_back_credentials("a", "t1", [_card("9001"), _card("9002")])
    [cred] = _cycle(a)
    assert cred.deactivate_date == datetime(2030, 1, 31, tzinfo=UTC)  # undated tokens: the enrollment's
    phone, watch = fake.tokens["a"]
    phone["attributes"]["extended_attributes"]["formatted_deactivate_date"] = "2031-06-30T00:00:00Z"
    watch["attributes"]["extended_attributes"]["formatted_deactivate_date"] = "2029-03-01T00:00:00Z"
    [cred] = _cycle(a)
    assert cred.deactivate_date == datetime(2029, 3, 1, tzinfo=UTC)  # the earliest end wins


def test_seos_unreadable_hub_token_date_holds_the_pass(seos):
    fake, a = seos()
    _cycle(a)
    a.write_back_credentials("a", "t1", [_card("9001")])
    fake.tokens["a"][0]["attributes"]["extended_attributes"]["formatted_deactivate_date"] = "next tuesday"
    [cred] = _cycle(a)
    assert cred.hold


# --- Seos: a card number already held in Unity ------------------------------------


def _number_fake(**others):
    """a enrolls; others are identities holding tokens of their own."""
    fake, a = _seos()
    for cn, tokens in others.items():
        fake.identities.append(_identity(cn))
        fake.tokens[cn] = tokens
    return fake, a


@pytest.fixture
def mailed(monkeypatch):
    from agsync import notifications

    sent = []
    monkeypatch.setattr(notifications, "notify_card_problem", lambda key, problem: sent.append(problem))
    return sent


def test_seos_number_held_by_another_identity_is_refused_and_mailed(tracked, mailed):
    tracked("a")
    fake, a = _number_fake(b=[_token("tb", internal="9001", embossed="", status="Inactive")])
    _cycle(a)
    assert not a.write_back_credentials("a", "t1", [_card("9001")])
    assert fake.posts == [] and written.all_tokens() == []
    assert "identity b already holds" in mailed[0]


def test_seos_loose_search_hit_without_the_number_does_not_block(tracked, mailed):
    tracked("a")
    fake, a = _number_fake(b=[_token("tb", internal="90012")])
    _cycle(a)
    assert a.write_back_credentials("a", "t1", [_card("9001")])
    assert mailed == []


@pytest.mark.parametrize("broken", ["fails", "ignored"])
def test_seos_number_check_that_cannot_answer_writes_nothing(tracked, broken):
    tracked("a")
    fake, a = _number_fake(**{f"p{i}": [] for i in range(30)})
    _cycle(a)
    if broken == "fails":
        fake.search_fails = True
    else:
        fake.internal_search_ignored = True
    assert not a.write_back_credentials("a", "t1", [_card("9001")])
    assert fake.posts == []


# --- AccessGrid status for a token or identity Unity no longer has ------------------


def test_status_for_a_token_no_longer_in_unity_is_mailed(mailed):
    fake = FakeUnity([_identity("a")], tokens={"a": []})
    assert not _adapter(fake).update_credential_status("a", "t1", CredentialStatus.SUSPENDED)
    assert "token t1 is no longer in Unity" in mailed[0]


def test_status_for_an_identity_no_longer_in_unity_is_mailed(mailed):
    fake = FakeUnity([], tokens={})
    fake.fail["/identities/gone/tokens.json"] = 404
    fake.fail["/identities/gone/tokens.xml"] = 404
    assert not _adapter(fake).update_credential_status("gone", "t1", CredentialStatus.SUSPENDED)
    assert "the identity is no longer in Unity" in mailed[0]


def test_status_that_fails_otherwise_is_logged_not_mailed(mailed):
    fake = FakeUnity([_identity("a")], tokens={"a": [_token("t1")]})
    fake.fail["/identities/a/tokens.json"] = 500
    fake.fail["/identities/a/tokens.xml"] = 500
    assert not _adapter(fake).update_credential_status("a", "t1", CredentialStatus.SUSPENDED)
    assert mailed == []


def test_seos_status_for_a_hub_token_no_longer_in_unity_is_mailed(seos, mailed):
    fake, a = seos()
    _cycle(a)
    a.write_back_credentials("a", "t1", [_card("9001"), _card("9002")])
    fake.tokens["a"] = fake.tokens["a"][1:]
    assert a.update_credential_status("a", "t1", CredentialStatus.SUSPENDED)
    assert "is no longer in Unity" in mailed[0]


# --- an accessgrid token that is not Active ---------------------------------------


def test_inactive_trigger_with_no_pass_is_held_not_issued(tracked):
    tracked("b")  # b holds a pass for tb; a second, Inactive trigger has none
    fake = FakeUnity([_identity("b")], tokens={"b": [_token("tb"), _token("t2", internal="7", status="Inactive")]},
                     details={"b": {"id": "b", "attributes": {}}})
    a = _adapter(fake)
    list(a.list_people())
    creds = {c.id: c for c in a.list_credentials("b")}
    assert creds["t2"].hold and not creds["tb"].hold


def test_inactive_trigger_holding_a_pass_is_not_held_so_the_pass_suspends(tracked):
    tracked("b")
    fake = FakeUnity([_identity("b")], tokens={"b": [_token("tb", status="Inactive")]},
                     details={"b": {"id": "b", "attributes": {}}})
    a = _adapter(fake)
    list(a.list_people())
    [cred] = a.list_credentials("b")
    assert not cred.hold and cred.status is CredentialStatus.SUSPENDED


def test_seos_inactive_enrollment_with_no_pass_is_held(seos):
    _, a = seos([_token("t1"), _token("t2", internal="7", status="Inactive")])
    creds = {c.id: c for c in _cycle(a)}
    assert creds["t2"].hold and not creds["t1"].hold


def test_an_email_that_is_not_an_address_is_left_off_a_desfire_pass():
    # AccessGrid rejects the whole issue over it, though the holder has a phone.
    fake = FakeUnity([_xml_identity("a", plasecidentityEmailaddress="n/a", plasecidentityPhone="5551234")],
                     tokens={"a": [_token("t1")]})
    _, people, (cred,) = _read(fake)
    assert (people["a"].email, people["a"].phone) == ("", "5551234")
    assert cred.trigger_active and not cred.hold


def test_retiring_a_card_deletes_the_token_created_without_its_id_recorded(seos):
    # Created in Unity, but the hub never recorded the id (a crash, or a
    # read-back that failed). Forgetting the row used to leave the token in
    # Unity, read from the next cycle on as an operator's enrollment.
    fake, a = seos([_token("t1"), _token("lost", internal="9001", status="Inactive")])
    written.add("a", "t1", "239", "9001", None, None)
    _cycle(a)
    assert a.retire_credentials("a", "t1", [_card("9001")]) == 1
    assert written.for_credential("a", "t1") == []
    assert all(t["id"] != "lost" for t in fake.tokens["a"])
    assert "lost" not in {c.id for c in _cycle(a)}


def test_retiring_an_unrecorded_token_waits_when_the_tokens_cannot_be_read(seos):
    fake, a = seos([_token("t1"), _token("lost", internal="9001", status="Inactive")])
    written.add("a", "t1", "239", "9001", None, None)
    _cycle(a)
    a._client.list_tokens = lambda pid: (_ for _ in ()).throw(UnityError("down"))
    assert a.retire_credentials("a", "t1", [_card("9001")]) == 0
    assert len(written.for_credential("a", "t1")) == 1, "kept, to retry next cycle"


def test_retiring_a_never_created_token_just_forgets_its_row(seos):
    fake, a = seos()
    written.add("a", "t1", "239", "9001", None, None)
    _cycle(a)
    assert a.retire_credentials("a", "t1", [_card("9001")]) == 1
    assert written.for_credential("a", "t1") == []
    assert not [p for p in fake.posts if p.get("_method") == "delete"]


def test_a_token_whose_pass_was_deleted_in_accessgrid_waits_to_be_switched_on(monkeypatch):
    # Released by phase 4, switched off: not issued again until an operator
    # switches it back on, and then issued as a new pass.
    monkeypatch.setattr(snapshot_mod, "list_cards", lambda *a: [])
    tracking.upsert(pacs_person_id="a", pacs_credential_id="t1", ag_card_id="pass-a", full_name="Jim Cooper",
                    employee_id="a", status="active", last_known_ag_state="active", sync_ref="ref-a")
    tracking.mark_deleted("a", "t1")
    fake = FakeUnity([_xml_identity("a", plasecidentityEmailaddress="j@x.co")],
                     tokens={"a": [_token("t1", status="Inactive")]})
    with pytest.raises(PacsRecordUnavailable):  # off and untracked: not even read, so nothing issued
        _read(fake)
    fake.tokens["a"] = [_token("t1")]
    _, _, (cred,) = _read(fake)
    assert not cred.hold and tracking.get("a", "t1").ag_card_id is None, "phase 1 issues it afresh"


def test_identity_search_pages_are_500_at_most():
    # A box capping pages below what we ask would end the search early.
    from agsync.lib.pacs.avigilon import client as client_mod

    assert client_mod.XML_PAGE_SIZE <= 500


@pytest.mark.parametrize("json_off", [False, True])
def test_a_full_page_of_tokens_is_not_taken_as_every_token(monkeypatch, json_off):
    # A short token list reads as tokens deleted, which retires passes.
    monkeypatch.setattr("agsync.lib.pacs.avigilon.client.TOKENS_PER_PAGE", 2)
    fake = FakeUnity([_identity("a")], tokens={"a": [_token("t1"), _token("t2")]})
    fake.json_off = json_off
    with pytest.raises(UnityError, match="full page"):
        _client(fake).list_tokens("a")
    fake.tokens["a"] = [_token("t1")]
    assert [t["id"] for t in _client(fake).list_tokens("a")] == ["t1"]


def test_a_token_list_short_of_its_own_total_is_not_taken_as_complete():
    import json

    fake = FakeUnity([_identity("a")], tokens={"a": [_token("t1")]})
    fake.raw["/identities/a/tokens.json"] = json.dumps({"tokens": [_token("t1")], "meta": {"recordsTotal": 3}})
    fake.fail["/identities/a/tokens.xml"] = 503
    with pytest.raises(UnityError, match="read 1 of 3"):
        _client(fake).list_tokens("a")
