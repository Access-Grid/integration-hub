"""A marker or number match never authorizes adoption or deletion."""

import httpx
import pytest

from agsync import notifications
from agsync.lib.pacs.avigilon import written
from agsync.lib.pacs.avigilon.client import UnityError
from agsync.sync import tracking

from .test_avigilon_unity import FakeUnity, _adapter, _card, _client, _cycle, _identity, _token


@pytest.fixture
def ownership_world(monkeypatch):
    fake = FakeUnity([_identity("a")], tokens={"a": [_token("enrollment", internal="")]})
    adapter = _adapter(fake)
    adapter.seos = True
    tracking.upsert(pacs_person_id="a", pacs_credential_id="enrollment", ag_card_id="pair",
                    full_name="Jim", employee_id="a", status="active")
    sent = []
    monkeypatch.setattr(notifications, "notify_card_problem", lambda key, problem: sent.append(problem))
    return fake, adapter, sent


@pytest.mark.parametrize("pending", [False, True])
def test_writeback_never_adopts_an_existing_untracked_token(ownership_world, pending):
    fake, adapter, sent = ownership_world
    fake.tokens["a"].append(_token("operator-owned", internal="9001", status="Inactive"))
    if pending:
        written.add("a", "enrollment", "239", "9001", None, None)
    _cycle(adapter)
    before = list(fake.tokens["a"])
    assert not adapter.write_back_credentials("a", "enrollment", [_card("9001")])
    assert fake.tokens["a"] == before and fake.posts == []
    assert all(e["token_id"] is None for e in written.all_tokens())
    assert sent and "Ownership is unknown" in sent[0]
    # Repeated offers must never turn uncertainty into ownership.
    assert not adapter.write_back_credentials("a", "enrollment", [_card("9001")])
    assert fake.posts == []


def test_retirement_keeps_pending_row_and_untracked_token(ownership_world):
    fake, adapter, sent = ownership_world
    fake.tokens["a"].append(_token("operator-owned", internal="9001"))
    written.add("a", "enrollment", "239", "9001", None, None)
    assert adapter.retire_credentials("a", "enrollment", [_card("9001")]) == 0
    assert {t["id"] for t in fake.tokens["a"]} == {"enrollment", "operator-owned"}
    assert len(written.all_tokens()) == 1 and fake.posts == []
    assert sent and "not adopting, changing or deleting" in sent[0]


def test_deleted_pass_release_preserves_ambiguous_and_known_tokens(ownership_world):
    fake, adapter, sent = ownership_world
    fake.tokens["a"].extend([_token("known", internal="9001"),
                             _token("known-off", internal="9003", status="Inactive"),
                             _token("operator-owned", internal="9002")])
    written.set_token(written.add("a", "enrollment", "239", "9001", None, None), "known")
    written.set_token(written.add("a", "enrollment", "239", "9003", None, None), "known-off")
    written.add("a", "enrollment", "239", "9002", None, None)
    _cycle(adapter)
    assert not adapter.release_deleted_pass("a", "enrollment")
    assert len(written.all_tokens()) == 3 and fake.posts == []
    assert len(fake.tokens["a"]) == 4 and sent


@pytest.mark.parametrize("record", ["none", "pending", "other-person", "other-token", "failed-issue"])
def test_client_refuses_delete_without_exact_tracking(ownership_world, record):
    fake, adapter, _ = ownership_world
    fake.tokens["a"].append(_token("operator-owned", internal="9001"))
    if record in ("pending", "other-person", "other-token"):
        row = written.add("b" if record == "other-person" else "a", "enrollment", "239", "9001", None, None)
        if record != "pending":
            written.set_token(row, "another-token" if record == "other-token" else "operator-owned")
    if record == "failed-issue":
        tracking.upsert(pacs_person_id="a", pacs_credential_id="operator-owned", ag_card_id=None,
                        full_name="Jim", employee_id="a", status="pending")
    with pytest.raises(UnityError, match="not tracked"):
        adapter._client.delete_token("a", "operator-owned")
    assert fake.posts == []
    assert any(t["id"] == "operator-owned" for t in fake.tokens["a"])


def test_pending_writeback_with_no_existing_token_can_retry(ownership_world):
    fake, adapter, _ = ownership_world
    written.add("a", "enrollment", "239", "9001", None, None)
    _cycle(adapter)
    assert adapter.write_back_credentials("a", "enrollment", [_card("9001")])
    [entry] = written.all_tokens()
    assert entry["token_id"] == fake.tokens["a"][0]["id"]
    assert entry["token_id"] != "enrollment"


def test_crash_after_create_never_authorizes_adoption_or_retirement(ownership_world, monkeypatch):
    fake, adapter, sent = ownership_world
    _cycle(adapter)

    def failed_record(*args):
        raise RuntimeError("crash before recording token ID")

    with monkeypatch.context() as patch:
        patch.setattr(written, "set_token", failed_record)
        with pytest.raises(RuntimeError, match="crash"):
            adapter.write_back_credentials("a", "enrollment", [_card("9001")])
    [pending] = written.all_tokens()
    assert pending["token_id"] is None
    before = list(fake.tokens["a"])
    posts = len(fake.posts)
    assert [c.id for c in _cycle(adapter)] == ["enrollment"]
    assert not adapter.write_back_credentials("a", "enrollment", [_card("9001")])
    assert adapter.retire_credentials("a", "enrollment", [_card("9001")]) == 0
    assert fake.tokens["a"] == before and len(fake.posts) == posts
    assert written.all_tokens() == [pending] and sent


def test_create_readback_cannot_claim_a_preexisting_token():
    fake = FakeUnity([_identity("a")], tokens={"a": [_token("operator-owned", internal="9001",
                                                         embossed="accessgrid", status="Inactive")]})
    fake.ignore_writes = True
    with pytest.raises(UnityError, match="ownership unconfirmed"):
        _client(fake).create_token("a", "9001", "accessgrid", "2")
    assert [t["id"] for t in fake.tokens["a"]] == ["operator-owned"]


@pytest.mark.parametrize("location,ours", [
    ("/identities/a/tokens/new-token_123?saved=true", True),
    ("/identities/a/tokens/new-token_123/edit", True),  # what the live box sent the probe
    # The redirect names no new token: the read-back alone decides. A live
    # box (Nov 2026) created the token and the hub refused it as not ours.
    ("/identities/a", True),
    ("", True),
    ("/identities/a/tokens/enrollment/edit", True),  # a token that was there before
    ("/identities/a/tokens/other-new/edit", False),  # another token that appeared meanwhile
])
def test_create_proves_ownership_by_the_read_back_not_the_redirect(location, ours):
    fake = FakeUnity([_identity("a")], tokens={"a": [_token("enrollment", internal="1", embossed="accessgrid")]})

    def transport(request):
        response = fake(request)
        if request.method == "POST" and request.url.path == "/identities/a/tokens":
            made = fake.tokens["a"][-1]
            made["id"] = made["attributes"]["cn"] = "new-token_123"
            fake.tokens["a"].append(_token("other-new", internal="555", embossed="someone"))
            response.headers["Location"] = location
        return response

    client = _client(fake)
    client._client = httpx.Client(base_url="https://unity", transport=httpx.MockTransport(transport))
    if ours:
        assert client.create_token("a", "9001", "accessgrid", "2") == "new-token_123"
    else:
        with pytest.raises(UnityError, match="ownership unconfirmed"):
            client.create_token("a", "9001", "accessgrid", "2")


def test_create_refuses_when_two_new_tokens_carry_the_number():
    fake = FakeUnity([_identity("a")])

    def transport(request):
        response = fake(request)
        if request.method == "POST" and request.url.path == "/identities/a/tokens":
            fake.tokens["a"].append(_token("twin", internal="9001", embossed="accessgrid", status="Inactive"))
        return response

    client = _client(fake)
    client._client = httpx.Client(base_url="https://unity", transport=httpx.MockTransport(transport))
    with pytest.raises(UnityError, match="2 new tokens"):
        client.create_token("a", "9001", "accessgrid", "2")


def test_a_refused_create_names_every_new_token_it_saw():
    fake = FakeUnity([_identity("a")])

    def transport(request):
        response = fake(request)
        if request.method == "POST" and request.url.path == "/identities/a/tokens":
            fake.tokens["a"].append(_token("twin", internal="9001", embossed="accessgrid", status="Inactive"))
        return response

    client = _client(fake)
    client._client = httpx.Client(base_url="https://unity", transport=httpx.MockTransport(transport))
    with pytest.raises(UnityError) as raised:
        client.create_token("a", "9001", "accessgrid", "2")
    message = str(raised.value)
    assert "new1 number='9001'" in message and "twin number='9001'" in message
    assert "redirect '/identities/a/tokens/new1'" in message


def test_create_ticks_download_so_the_token_reaches_the_controllers():
    fake = FakeUnity([_identity("a")])
    token_id = _client(fake).create_token("a", "9001", "accessgrid", "2")
    [form] = fake.posts
    assert form["token[plasecDownload]"] == "TRUE", "what Unity's own form posts when the box is ticked"
    [made] = [t for t in fake.tokens["a"] if t["id"] == token_id]
    assert made["attributes"]["plasecDownload"] == "TRUE"


def test_a_token_that_reads_back_without_download_is_kept_and_reported(caplog):
    fake = FakeUnity([_identity("a")])

    def transport(request):
        response = fake(request)
        if request.method == "POST" and request.url.path == "/identities/a/tokens":
            fake.tokens["a"][-1]["attributes"]["plasecDownload"] = "FALSE"
        return response

    client = _client(fake)
    client._client = httpx.Client(base_url="https://unity", transport=httpx.MockTransport(transport))
    assert client.create_token("a", "9001", "accessgrid", "2") == "new1", "refusing would strand the pass"
    assert "reads Download='FALSE', not TRUE" in caplog.text
