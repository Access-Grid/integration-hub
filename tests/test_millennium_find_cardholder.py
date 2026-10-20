"""Finding a cardholder's record id from their Employee ID.

The search screen posts a form and answers with a page whose results sit in
a script as JSON: `var data = [{"ID": 12478, ..., "EmployeeID": "22344"}];`.
"""

from __future__ import annotations

import json

import httpx
import pytest

from agsync.lib.pacs.millennium_ultra.client import MillenniumError, MillenniumUltraClient

TOKEN = "search-token-456"
SEARCH_PAGE = f'<form><input name="__RequestVerificationToken" type="hidden" value="{TOKEN}"/></form>'


def _results(*rows):
    return (
        "<html><script>var colums = [];\n var ViewMode = 2;\n"
        f"            var data = {json.dumps(list(rows))};\n        </script></html>"
    )


@pytest.fixture
def searcher(monkeypatch):
    def _build(results_page):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == "/Cardholders/Cardholders/Search":
                return httpx.Response(200, text=SEARCH_PAGE)
            if request.url.path == "/Cardholders/Cardholders/SearchResult":
                return httpx.Response(200, text=results_page)
            return httpx.Response(404)

        client = MillenniumUltraClient(
            base_url="https://hosted105.mgiaccess.test", auth_cookie="session"
        )
        client._http = httpx.Client(transport=httpx.MockTransport(handler))
        monkeypatch.setattr(client, "_pace", lambda: None)
        return client, seen

    return _build


def test_the_record_id_comes_from_the_results(searcher):
    client, seen = searcher(_results({"ID": 12478, "EmployeeID": "22344"}))
    assert client.find_cardholder_id("22344") == "12478"
    body = seen[1].content.decode()
    assert 'name="EmployeeID"\r\n\r\n22344\r\n' in body
    assert f'name="__RequestVerificationToken"\r\n\r\n{TOKEN}\r\n' in body
    assert seen[1].headers["content-type"].startswith("multipart/form-data")


def test_only_an_exact_match_counts(searcher):
    """A search need not match exactly; the record we act on must."""
    client, _ = searcher(_results(
        {"ID": 1, "EmployeeID": "223440"}, {"ID": 2, "EmployeeID": "22344"},
    ))
    assert client.find_cardholder_id("22344") == "2"


@pytest.mark.parametrize("rows, count", [
    ((), 0),
    (({"ID": 1, "EmployeeID": "22344"}, {"ID": 2, "EmployeeID": "22344"}), 2),
])
def test_anything_but_one_match_is_refused(searcher, rows, count):
    client, _ = searcher(_results(*rows))
    with pytest.raises(MillenniumError, match=f"matches {count} cardholders"):
        client.find_cardholder_id("22344")


def test_a_page_without_results_is_refused(searcher):
    client, _ = searcher("<html>Session expired</html>")
    with pytest.raises(MillenniumError, match="did not return results"):
        client.find_cardholder_id("22344")


# --- deleting a card by its number ---------------------------------------


@pytest.fixture
def deleter(monkeypatch, millennium_page):
    """Search finds record 11587, whose page is the captured one: card 1/1
    in slot 1 (CardID 7919), card 2/2 in slot 2 (CardID 7922)."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/Cardholders/Cardholders/Search":
            return httpx.Response(200, text=SEARCH_PAGE)
        if path == "/Cardholders/Cardholders/SearchResult":
            return httpx.Response(200, text=_results({"ID": 11587, "EmployeeID": "22344"}))
        if path == "/Cardholders/Cardholders/Index/11587":
            return httpx.Response(200, text=millennium_page)
        if path == "/Cardholders/Cardholders/DeleteCard":
            return httpx.Response(200, text="true")
        return httpx.Response(404)

    client = MillenniumUltraClient(
        base_url="https://hosted105.mgiaccess.test", auth_cookie="session"
    )
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(client, "_pace", lambda: None)
    return client, seen


def test_the_card_is_deleted_from_its_record_by_its_card_id(deleter):
    client, seen = deleter
    assert client.delete_card_by_number("22344", "2", "2") is True
    [delete] = [r for r in seen if r.url.path.endswith("/DeleteCard")]
    form = dict(pair.split("=", 1) for pair in delete.content.decode().split("&"))
    assert (form["ID"], form["CardID"]) == ("11587", "7922")
    assert form["__RequestVerificationToken"], "the page's own token"


def test_a_card_not_on_the_cardholder_is_refused(deleter):
    client, seen = deleter
    with pytest.raises(MillenniumError, match="holds no card 2/999"):
        client.delete_card_by_number("22344", "2", "999")
    assert not [r for r in seen if r.url.path.endswith("/DeleteCard")]
