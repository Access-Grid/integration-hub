"""Avigilon Unity: reads that fail raise, writes that can't echo refuse.

Runs the real client against a fake Unity behind httpx.MockTransport, so
the session handling, paging and form building are all exercised — only
the appliance is pretend. Response shapes follow what the avigilon-service
agent captured from a live server.
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qs

import httpx
import pytest

from agsync.lib.pacs.avigilon.adapter import AvigilonAdapter, parse_date
from agsync.lib.pacs.avigilon.client import (
    TOKEN_FORM_DATES,
    TOKEN_FORM_FIELDS,
    UnityAuthError,
    UnityClient,
    UnityError,
)
from agsync.lib.pacs.base import CredentialStatus, PacsRecordUnavailable


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
        self.json_off = False  # a box with only the XML endpoints
        self.gets = []
        self.raw = {}  # path -> body served verbatim once signed in

    @staticmethod
    def _xml(root, row, rows):
        body = "".join(f"<{row}>" + "".join(f"<{k}>{v}</{k}>" for k, v in r.items()) + f"</{row}>" for r in rows)
        return httpx.Response(200, text=f"<{root}>{body}</{root}>", headers={"content-type": "application/xml"})

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET":
            self.gets.append(path)
        if path in self.fail:
            return httpx.Response(self.fail[path])
        if path in self.raw and self.signed_in:
            return httpx.Response(200, text=self.raw[path])
        if self.json_off and path.endswith(".json"):
            return httpx.Response(404)
        if path == "/sessions" and req.method == "POST":
            kind = "json" if "json" in req.headers.get("content-type", "") else "form"
            self.signed_in = kind == self.accept
            return httpx.Response(302 if kind == "form" else 200, headers={"Location": "/"})
        if path == "/sessions/new":
            return httpx.Response(200, text="<html>sign in</html>")
        if not self.signed_in:
            return httpx.Response(302, headers={"Location": "https://unity/sessions/new"})
        if path == "/":
            return httpx.Response(200, text="dashboard")
        if path == "/identities.json":
            page, per = int(req.url.params.get("page", 1)), int(req.url.params.get("perpage", 100))
            start = 0 if self.per_page_cap else (page - 1) * per
            rows = self.identities[start:start + per]
            return httpx.Response(200, json={"data": rows, "meta": {"recordsFiltered": len(self.identities)}})
        parts = path.strip("/").split("/")
        if path == "/identities.xml":
            return self._xml("identities", "identity", self.identities)
        if parts[-1] == "tokens.xml":
            rows = [{k: v for k, v in t["attributes"].items() if k != "extended_attributes"}
                    | {"plasecTokenstatus": {"Active": "1", "Inactive": "2"}[
                        t["attributes"]["extended_attributes"]["token_status"]]}
                    for t in self.tokens.get(parts[1], [])]
            return self._xml("tokens", "token", rows)
        if parts[-1] == "tokens.json":
            return httpx.Response(200, json={"tokens": self.tokens.get(parts[1], []), "recordsTotal": 0})
        if path.endswith(".json") and len(parts) == 2:
            return httpx.Response(200, json={"data": self.details[parts[1][:-5]]})
        if req.method == "POST" and len(parts) == 4:
            form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
            self.posts.append(form)
            for t in [] if self.ignore_writes else self.tokens[parts[1]]:
                if t["id"] == parts[3]:
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


def test_failed_roster_raises_from_list_people():
    fake = FakeUnity(identities=[_identity("a")])
    c = _adapter(fake)
    c._client.login()
    fake.fail["/identities.json"] = 500
    fake.fail["/identities.xml"] = 500
    with pytest.raises(UnityError):
        list(c.list_people())
    # Same outage met while signing in: an error, not a "wrong password".
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
    fake = FakeUnity([_identity("a", status="Pending review")], tokens={"a": []})
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


def test_write_refused_when_unity_did_not_return_a_field():
    token = _token("t1")
    del token["attributes"]["plasecTokenVIP"]
    fake = FakeUnity([_identity("a")], tokens={"a": [token]})
    assert not _adapter(fake).update_credential_status("a", "t1", CredentialStatus.SUSPENDED)
    assert fake.posts == []


def test_write_that_does_not_stick_is_a_failure():
    fake = FakeUnity([_identity("a")], tokens={"a": [_token("t1")]})
    fake.ignore_writes = True  # answers 302, changes nothing
    assert not _adapter(fake).update_credential_status("a", "t1", CredentialStatus.SUSPENDED)
