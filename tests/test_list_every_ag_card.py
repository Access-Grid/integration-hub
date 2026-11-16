"""Every pass on the template, not just the first page.

Seen on-site: the SDK's access_cards.list asks for page 1 only, and the API
answers 25 by default, so the snapshot never saw passes past the 25th. Their
installs were never switched on in the PACS, and their deletions never seen.
"""

from __future__ import annotations

from agsync.ag import CARDS_PER_PAGE, list_cards


class _Api:
    def __init__(self, total):
        self.keys = [{"id": f"card-{n}", "metadata": {}} for n in range(total)]
        self.asked: list[dict] = []

    def _get(self, endpoint, params=None):
        self.asked.append(params)
        size, page = params["per_page"], params["page"]
        pages = max(1, -(-len(self.keys) // size))
        return {
            "keys": self.keys[(page - 1) * size: page * size],
            "page": page, "total_pages": pages,
        }


def test_every_page_is_read():
    api = _Api(total=230)
    cards = list_cards(api, "tpl-1")
    assert [c.id for c in cards] == [f"card-{n}" for n in range(230)]
    assert [p["page"] for p in api.asked] == [1, 2, 3]
    assert {p["per_page"] for p in api.asked} == {CARDS_PER_PAGE}
    assert {p["template_id"] for p in api.asked} == {"tpl-1"}


def test_an_empty_template_is_one_request():
    api = _Api(total=0)
    assert list_cards(api, "tpl-1") == []
    assert len(api.asked) == 1
