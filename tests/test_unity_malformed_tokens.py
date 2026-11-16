"""An invalid token list is unknown, never evidence for revocation."""

import copy
import json

import pytest

from agsync.lib.pacs.avigilon import written
from agsync.lib.pacs.avigilon.client import UnityError
from agsync.sync import tracking

from .test_avigilon_unity import FakeUnity, _client, _identity, _token
from .test_unity_spec_end_to_end import (  # noqa: F401 — shared engine fixtures
    _desfire,
    _seos,
)
from .test_unity_spec_end_to_end import (
    mailed as _mailed,
)
from .test_unity_spec_end_to_end import (
    world as _world,
)

mailed, world = _mailed, _world


@pytest.mark.parametrize("items", [
    [None], [42], [_token("known"), None], [{}],
    [{"id": "  "}], [{"id": []}], [{"id": True}],
    [{"id": "known", "attributes": None}],
    [{"id": "known"}],
    [{"id": "known", "extended_attributes": ["broken"]}],
    [{"id": "known", "extended_attributes": []}],
    [_token("known", status="unknown")],
    [_token("known"), _token("known")],
])
def test_bad_json_token_entries_make_the_entire_read_unavailable(items):
    unity = FakeUnity([_identity("a")])
    unity.raw["/identities/a/tokens.json"] = json.dumps({"tokens": items})
    unity.fail["/identities/a/tokens.xml"] = 503
    with pytest.raises(UnityError):
        _client(unity).list_tokens("a")


@pytest.mark.parametrize("meta", [
    {"recordsTotal": 2}, {"recordsTotal": 0}, {"recordsTotal": -1},
    {"recordsTotal": "unknown"}, {"recordsTotal": True},
    {"recordsTotal": 1.5}, {"recordsFiltered": 2, "recordsTotal": 1},
    {"recordsFiltered": 1, "recordsTotal": 2},
    {"recordsFiltered": 0, "recordsTotal": 1},
    ["broken"], [], False,
])
def test_bad_or_incomplete_counts_make_the_read_unavailable(meta):
    unity = FakeUnity([_identity("a")])
    unity.raw["/identities/a/tokens.json"] = json.dumps({"tokens": [_token("known")], "meta": meta})
    unity.fail["/identities/a/tokens.xml"] = 503
    with pytest.raises(UnityError):
        _client(unity).list_tokens("a")


@pytest.mark.parametrize("xml", [
    "<tokens><unexpected/></tokens>", "<tokens>broken</tokens>",
    "<tokens><token/></tokens>", "<tokens><token><cn> </cn></token></tokens>",
    "<tokens><token><cn>known</cn></token><unexpected/></tokens>",
])
def test_bad_xml_token_entries_make_the_read_unavailable(xml):
    unity = FakeUnity([_identity("a")])
    unity.fail["/identities/a/tokens.json"] = 503
    unity.raw["/identities/a/tokens.xml"] = xml
    with pytest.raises(UnityError):
        _client(unity).list_tokens("a")


def test_a_valid_fallback_can_replace_an_invalid_json_read():
    unity = FakeUnity([_identity("a")], tokens={"a": [_token("known")]})
    unity.raw["/identities/a/tokens.json"] = '{"tokens": [null]}'
    assert [t["id"] for t in _client(unity).list_tokens("a")] == ["known"]


@pytest.mark.parametrize("protocol", ["seos", "desfire"])
@pytest.mark.parametrize("pair", [False, True])
def test_malformed_reads_preserve_pass_tokens_and_tracking_then_recover(world, protocol, pair):
    unity, ag, cycle = _seos(world, pair) if protocol == "seos" else _desfire(world, pair)
    cycle()
    pass_id = ag.only_pass()
    ag.install(pass_id)
    cycle()
    before = copy.deepcopy(unity.tokens), written.all_tokens(), tracking.all_tracked(), list(ag.calls)
    unity.raw["/identities/a/tokens.json"] = '{"tokens": [null]}'
    unity.raw["/identities/a/tokens.xml"] = "<tokens><token/></tokens>"
    cycle()
    cycle()
    assert (unity.tokens, written.all_tokens(), tracking.all_tracked(), ag.calls) == before

    unity.raw.clear()
    cycle()
    assert ag.calls == before[3]
    # A genuinely empty, successfully read list still permits revocation.
    unity.tokens["a"] = []
    cycle()
    assert ag.verbs("delete") == [("delete", pass_id)]
