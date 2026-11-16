"""The probe carries its own copy of what the Unity adapter reads and writes,
so the exe can run without the rest of the hub. These keep the copy honest."""

import importlib.util
from pathlib import Path

from agsync.lib.pacs.avigilon import adapter, client

_spec = importlib.util.spec_from_file_location(
    "avigilon_probe", Path(__file__).parent.parent / "scripts" / "avigilon_probe.py")
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


def test_probe_expects_the_fields_the_client_writes():
    assert probe.TOKEN_FORM_FIELDS == client.TOKEN_FORM_FIELDS
    assert probe.TOKEN_FORM_DATES == client.TOKEN_FORM_DATES
    assert probe._PREFIXES == client._PREFIXES


def test_probe_reads_statuses_dates_and_searches_as_the_adapter_does():
    assert probe.STATUS_CODES == client._STATUS_CODES
    assert probe.DATE_FORMATS == adapter._DATE_FORMATS
    assert probe._XML_SEARCH == client.XML_IDENTITY_SEARCH
    assert probe._EMBOSSED_SEARCH == client.EMBOSSED_SEARCH
    assert probe._INTERNAL_SEARCH == client.INTERNAL_SEARCH
    assert probe._XML_PAGE == client.XML_PAGE_SIZE
    assert probe._MAX_NUMBER_HITS == adapter.MAX_NUMBER_HITS


def test_detail_mode_compares_presence_and_agreement_never_values():
    import json

    import httpx

    listed = {"1": {"plasecidentityTitle": "Coach", "plasecidentityEmailaddress": "ann@x.test"},
              "2": {"plasecidentityDepartment": "Office"}}
    detail = {"1": {"plasecidentityTitle": "Coach", "plasecidentityEmailaddress": "ann@x.test"},
              "2": {}}  # a detail that lacks what the list has

    def row(cn, fields):
        return f"<identity><cn>{cn}</cn>" + "".join(f"<{k}>{v}</{k}>" for k, v in fields.items()) + "</identity>"

    def serve(req):
        path, q = req.url.path, dict(req.url.params)
        if path == "/identities.xml":
            ids = [q["id"]] if q.get("id") else ["0", "9", "1", "2"]
            rows = {"0": {}, "9": {}, **listed}
            return httpx.Response(200, text="<identities>" + "".join(row(i, rows[i]) for i in ids) + "</identities>")
        cn, ext = path.rsplit("/", 1)[1].split(".")
        if ext == "json":
            return httpx.Response(200, json={"data": {"id": cn, "attributes": {"cn": cn, **detail[cn]}}})
        return httpx.Response(200, text="<identities>" + row(cn, detail[cn]) + "</identities>")

    p = probe.Probe("unity", "u", "pw", mode="detail")
    p.http = httpx.Client(base_url="https://unity", transport=httpx.MockTransport(serve))
    p.detail_vs_list(samples=2)
    out = p.report["detail_vs_list"]
    assert list(p.report)[4] == "detail_vs_list"
    assert out["cardholders_compared"] == 2
    assert out["summary"]["identityTitle"] == {"list": 1, "detail_json": 1, "detail_xml": 1,
                                               "json_agrees": 2, "xml_agrees": 2}
    assert out["summary"]["identityDepartment"] == {"list": 1, "detail_json": 0, "detail_xml": 0,
                                                    "json_agrees": 1, "xml_agrees": 1}
    assert out["list_search_by_id"] == {"answered": True, "rows": 1, "only_that_identity": True}
    text = json.dumps(out)
    assert "Coach" not in text and "Office" not in text and "ann@x.test" not in text


# -- full mode: does the hub accept each holder's token list? ------------------

import json as _json  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

from .test_avigilon_unity import FakeUnity, _client, _identity, _token  # noqa: E402

# Every token list the client is tested to reject (test_unity_malformed_tokens),
# a few it accepts, and the edges between.
_JSON_ITEMS = [
    [None], [42], [_token("known"), None], [{}],
    [{"id": "  "}], [{"id": []}], [{"id": True}],
    [{"id": "known", "attributes": None}], [{"id": "known"}],
    [{"id": "known", "extended_attributes": ["broken"]}],
    [{"id": "known", "extended_attributes": []}],
    [_token("known", status="unknown")], [_token("known", status="Activo")],
    [_token("known"), _token("known")],
    [], [_token("known")], [_token("a1"), _token("a2", status="Inactive")],
    [_token("a1", status="Not Yet Active"), _token("a2", status="Expired")],
    [_token(f"t{i}") for i in range(500)],
]
_JSON_META = [
    None, {}, {"recordsTotal": 1}, {"recordsFiltered": 1, "recordsTotal": 1}, {"recordsTotal": "1"},
    {"recordsTotal": 2}, {"recordsTotal": 0}, {"recordsTotal": -1}, {"recordsTotal": "unknown"},
    {"recordsTotal": True}, {"recordsTotal": 1.5}, {"recordsFiltered": 2, "recordsTotal": 1},
    {"recordsFiltered": 1, "recordsTotal": 2}, {"recordsFiltered": 0, "recordsTotal": 1},
    ["broken"], [], False,
]
_XML = [
    "<tokens><unexpected/></tokens>", "<tokens>broken</tokens>",
    "<tokens><token/></tokens>", "<tokens><token><cn> </cn></token></tokens>",
    "<tokens><token><cn>known</cn></token><unexpected/></tokens>",
    "<tokens/>", "<nil-classes/>", "<nil-classes><x/></nil-classes>", "<html>sign in</html>", "not xml",
    "<tokens><token><cn>t1</cn><plasecTokenstatus>Active</plasecTokenstatus></token></tokens>",
    "<token><cn>t1</cn><plasecTokenstatus>2</plasecTokenstatus></token>",
    "<tokens><token><cn>t1</cn><plasecTokenstatus>Activo</plasecTokenstatus></token></tokens>",
    "<tokens><token><cn>t1</cn><plasecTokenstatus>Active</plasecTokenstatus></token>"
    "<token><cn>t1</cn><plasecTokenstatus>Active</plasecTokenstatus></token></tokens>",
]


def _client_accepts(raw: dict) -> bool:
    unity = FakeUnity([_identity("a")])
    for fmt in ("json", "xml"):
        path = f"/identities/a/tokens.{fmt}"
        if fmt in raw:
            unity.raw[path] = raw[fmt]
        else:
            unity.fail[path] = 503
    try:
        _client(unity).list_tokens("a")
    except Exception:  # noqa: BLE001 — any refusal is a refusal
        return False
    return True


@pytest.mark.parametrize("items", _JSON_ITEMS, ids=range(len(_JSON_ITEMS)))
@pytest.mark.parametrize("meta", _JSON_META[:6], ids=range(6))
def test_probe_judges_a_json_token_list_as_the_client_does(items, meta):
    body = {"tokens": items} if meta is None else {"tokens": items, "meta": meta}
    problem, _ = probe.json_token_read_problem(body)
    assert (problem is None) == _client_accepts({"json": _json.dumps(body)}), problem


@pytest.mark.parametrize("meta", _JSON_META, ids=range(len(_JSON_META)))
def test_probe_judges_json_counts_as_the_client_does(meta):
    body = {"tokens": [_token("known")], "meta": meta}
    problem, _ = probe.json_token_read_problem(body)
    assert (problem is None) == _client_accepts({"json": _json.dumps(body)}), problem


@pytest.mark.parametrize("xml", _XML, ids=range(len(_XML)))
def test_probe_judges_an_xml_token_list_as_the_client_does(xml):
    problem, _ = probe.xml_token_read_problem(xml)
    assert (problem is None) == _client_accepts({"xml": xml}), problem


def test_full_mode_reads_only_and_counts_holders_the_hub_could_not_read(monkeypatch):
    good = {"tokens": [_token("t1", embossed="accessgrid")], "meta": {"recordsTotal": 1}}
    bad_json = {"tokens": [_token("t1", status="Activo")]}
    bad_xml = "<tokens><token><cn>t1</cn><plasecTokenstatus>Activo</plasecTokenstatus></token></tokens>"
    sent = []

    def serve(req):
        sent.append((req.method, req.url.path))
        path, q = req.url.path, dict(req.url.params)
        if path == "/identities.xml":
            ids = ["good", "bad"] if q.get("adv_search_val_0") == "accessgrid" else []
            return httpx.Response(200, text="<identities>" + "".join(
                f"<identity><cn>{i}</cn></identity>" for i in ids) + "</identities>")
        if path == "/identities/good/tokens.json":
            assert q == {"page": "1", "perpage": "500"}
            return httpx.Response(200, json=good)
        if path == "/identities/bad/tokens.json":
            return httpx.Response(200, json=bad_json)
        if path == "/identities/bad/tokens.xml":
            return httpx.Response(200, text=bad_xml)
        return httpx.Response(404, text="nope")

    p = probe.Probe("unity", "u", "pw", mode="full")
    p.http = httpx.Client(base_url="https://unity", transport=httpx.MockTransport(serve))
    monkeypatch.setattr(p, "accessgrid", lambda: None)
    monkeypatch.setattr(p, "login", lambda: True)  # the sign-in POST is the one write-shaped call, tested apart
    report = p.run_full(samples=2)

    assert {m for m, _ in sent} == {"GET"}, "nothing but reads after signing in"
    out = report["hub_token_reads"]
    assert list(report)[4] == "hub_token_reads", "the answer heads the report"
    assert (out["identities_checked"], out["enrolled_holders_among_them"]) == (2, 2)
    assert (out["hub_reads"], out["hub_cannot_read"]) == (1, 1)
    assert out["json"] == {"accepted": 1, "unrecognised status": 1}
    assert out["xml"] == {"HTTP 404": 1, "unrecognised status": 1}
    assert out["statuses_seen"] == {"Active": 1, "Activo": 1}
    assert out["count_keys_in_json"] == {"recordsTotal": 1, "none": 1}
    assert report["steps"]["hub_tokens_xml:bad"]["verdict"] == "unrecognised status"
    assert "hub_tokens_json:good" not in report["steps"], "accepted reads are left out"


def test_internal_search_compares_both_searches_on_real_active_and_inactive_tokens(monkeypatch):
    """Numbers come off tokens Unity returned; the advanced search finds no
    one (as live), the Token box finds the holder, and part of a number is
    checked only against the Token box. Reads only, numbers never reported."""
    tokens = {"a1": [("1111", "1")], "i1": [("2222", "2")], "n1": []}
    sent = []

    def serve(req):
        sent.append(req.method)
        path, q = req.url.path, dict(req.url.params)
        if path == "/identities.xml":
            if q.get("adv_search_val_0") or not (q.get("tkn") or q.get("page")):
                return httpx.Response(200, text='<nil-classes type="array"/>')
            tkn = q.get("tkn", "")
            ids = (["a1", "n1", "i1"] if not tkn and q["page"] == "1"
                   else [cn for cn, ts in tokens.items() if tkn and any(tkn in n for n, _ in ts)])
            status = {"i1": "2"}
            return httpx.Response(200, text="<identities>" + "".join(
                f"<identity><cn>{i}</cn><plasecIdstatus>{status.get(i, '1')}</plasecIdstatus></identity>"
                for i in ids) + "</identities>")
        cn = path.split("/")[2]
        return httpx.Response(200, text="<tokens>" + "".join(
            f"<token><cn>tok{s}</cn><plasecInternalnumber>{n}</plasecInternalnumber>"
            f"<plasecTokenstatus>{s}</plasecTokenstatus></token>" for n, s in tokens[cn]) + "</tokens>")

    p = probe.Probe("unity", "u", "pw", mode="search")
    p.http = httpx.Client(base_url="https://unity", transport=httpx.MockTransport(serve))
    p.internal_search()
    out = p.report["internal_search"]

    assert set(sent) == {"GET"}
    assert out["identities_scanned"] == 2, "the inactive identity is read first, then until both are found"
    assert out["adv"]["active_token"] == {"hits": 0, "holder_found": False}
    assert out["adv"]["verdict"].startswith("does not find the holder")
    assert out["tkn"]["active_token"] == {"hits": 1, "holder_found": True}
    assert out["tkn"]["inactive_token"] == {"hits": 1, "holder_found": True}
    assert out["tkn"]["verdict"] == "finds the holder of an Active token"
    assert out["tkn"]["matching"] == {"first_digits": {"hits": 1, "holder_found": True},
                                      "last_digits": {"hits": 1, "holder_found": True},
                                      "extra_digit": {"hits": 0, "holder_found": False}}
    assert "1111" not in _json.dumps(p.report) and "2222" not in _json.dumps(p.report)


_NEW_IDENTITY_PAGE = """<html><head><meta name="csrf-token" content="tok"></head><body>
<form action="/identities" method="get"><input name="lnam" value=""></form>
<form action="/identities" accept-charset="UTF-8" method="post"><input name="utf8" type="hidden" value="&#x2713;">
<input type="hidden" name="authenticity_token" value="tok"><input name="identity[plasecFname]">
<input name="identity[plasecLname]"><select name="identity[plasecTyp]"><option value="Employee">E</option></select>
<input type="checkbox" name="identity[plasecVIP]" value="1"><input type="submit" value="Save"></form></body></html>"""


def _write_unity(fail_token_delete=False, lname_override=None):
    """A Unity that keeps one identity and its tokens, for `write` mode."""
    state = {"identity": None, "tokens": {}, "posts": []}

    def serve(req):
        path, q = req.url.path, dict(req.url.params)
        if req.method == "POST":
            form = dict(httpx.QueryParams(req.content.decode()))
            state["posts"].append((path, form, req.headers.get("x-csrf-token")))
            if path == "/identities":
                state["identity"] = form
                return httpx.Response(302, headers={"location": "https://unity/identities/abcdef0123456789/edit"})
            if path.endswith("/tokens"):
                state["tokens"]["tk1"] = form
                return httpx.Response(302, headers={"location": "https://unity/identities/abcdef0123456789/tokens/tk1/edit"})
            if form.get("_method") == "delete" and "/tokens/" in path:
                if not fail_token_delete:
                    state["tokens"].pop(path.rsplit("/", 1)[1], None)
                return httpx.Response(302, headers={"location": "/"})
            if form.get("_method") == "delete":
                state["identity"] = None
                return httpx.Response(302, headers={"location": "/identities"})
        if path == "/identities/new":
            return httpx.Response(200, text=_NEW_IDENTITY_PAGE)
        if path == "/identities.xml":
            return httpx.Response(200, text='<nil-classes type="array"/>' if q.get("tkn") else "<identities/>")
        if path == "/identities/abcdef0123456789.json":
            if state["identity"] is None:
                return httpx.Response(404, json={"errors": ["Identity was not found."]})
            ident = state["identity"]
            return httpx.Response(200, json={"id": "abcdef0123456789", "attributes": {
                "plasecLname": lname_override or ident["identity[plasecLname]"],
                "plasecIdstatus": int(ident["identity[plasecIdstatus]"])}})
        if path == "/identities/abcdef0123456789/tokens.xml":
            return httpx.Response(200, text="<tokens>" + "".join(
                f"<token><cn>{tid}</cn><plasecInternalnumber>{f['token[plasecInternalnumber]']}</plasecInternalnumber>"
                f"<plasecEmbossednumber>{f['token[plasecEmbossednumber]']}</plasecEmbossednumber>"
                f"<plasecTokenstatus>{f['token[plasecTokenstatus]']}</plasecTokenstatus></token>"
                for tid, f in state["tokens"].items()) + "</tokens>")
        return httpx.Response(404)

    p = probe.Probe("unity", "u", "pw", mode="write")
    p.http = httpx.Client(base_url="https://unity", transport=httpx.MockTransport(serve))
    return p, state


def test_write_mode_creates_an_inactive_identity_and_token_then_deletes_both():
    p, state = _write_unity()
    p.write_test()
    out = p.report["write_test"]

    assert out["verdict"] == "create and delete both work"
    assert out["left_in_unity"] == []
    created = state["posts"][0][1]
    assert created["identity[plasecLname]"].startswith("Probe ") and created["identity[plasecIdstatus]"] == "2"
    assert created["identity[plasecTyp]"] == "Employee", "the form's own defaults are posted back"
    assert "identity[plasecVIP]" not in created and "lnam" not in created, "unchecked box and search form left out"
    token = state["posts"][1][1]
    assert token["token[plasecEmbossednumber]"] == "agsync-probe" and token["token[plasecTokenstatus]"] == "2"
    assert all(csrf == "tok" and form["authenticity_token"] == "tok" for _, form, csrf in state["posts"])
    assert [form.get("_method") for _, form, _ in state["posts"]] == [None, None, "delete", "delete"]
    assert out["token_created"]["location_names_it"] and out["identity_deleted"]["gone"]
    assert token["token[plasecInternalnumber]"] not in _json.dumps(p.report)


def test_write_mode_names_what_it_could_not_delete():
    p, _ = _write_unity(fail_token_delete=True)
    p.write_test()
    out = p.report["write_test"]
    assert out["token_deleted"]["gone"] is False
    assert out["left_in_unity"][0] == "token tk1 on identity abcdef0123456789"


def test_write_mode_never_deletes_an_identity_it_cannot_prove_it_made():
    p, state = _write_unity(lname_override="Someone Else")
    p.write_test()
    assert [form.get("_method") for _, form, _ in state["posts"]] == [None], "no delete sent"
    assert p.report["write_test"]["left_in_unity"] == []
    assert "nothing deleted" in p.report["write_test"]["verdict"]
    assert p.report["write_test"]["check_by_hand"].startswith("identity abcdef0123456789")
