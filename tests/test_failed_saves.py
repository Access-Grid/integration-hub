"""Keeping the exchange behind a refused save.

The log says a cardholder's save was rejected, and usually why — but the
PACS does not always say why. Cardholder 12247 is refused on every cycle
and the page it sends back carries no validation message at all, which
leaves a sentence and nothing to act on. So the request and the response
are kept, and there is a tab to read them in.
"""

from __future__ import annotations

import httpx
import pytest

from agsync import failed_saves


def _reset_connection() -> None:
    """Actually drop the database connection.

    The module keeps `_initialized` and a thread-local `conn`; there is no
    `_conn`, so the fixture copied around the rest of the suite sets an
    attribute nobody reads and leaves every test sharing one database.
    """
    from agsync.db import connection

    conn = getattr(connection._local, "conn", None)
    if conn is not None:
        conn.close()
        del connection._local.conn
    connection._initialized = False


@pytest.fixture
def db(tmp_path, monkeypatch):
    from agsync.config import get_settings

    monkeypatch.setenv("AG_SYNC_DB_PATH", str(tmp_path / "saves.db"))
    get_settings.cache_clear()
    _reset_connection()
    yield
    _reset_connection()
    get_settings.cache_clear()


def _record(**kw):
    defaults = dict(
        vendor="millennium_ultra", cardholder_id="12247", status_code=200,
        reasons=["Card_1_EncodedCardNumber: duplicated"],
        request="name=\"EmployeeID\"\r\n\r\n33137",
        response="<html>the page back</html>",
    )
    defaults.update(kw)
    failed_saves.record(**defaults)


# =====================================================================
# Keeping them
# =====================================================================


def test_a_refusal_is_kept_whole(db):
    _record()

    (row,) = failed_saves.recent()
    assert row["cardholder_id"] == "12247"
    assert row["status_code"] == 200
    assert "33137" in row["request"]
    assert "the page back" in row["response"]
    assert "duplicated" in row["reasons"]


def test_a_refusal_with_no_reason_is_still_kept(db):
    """The case this exists for — nothing to say, so the exchange is all
    there is."""
    _record(reasons=[])

    (row,) = failed_saves.recent()
    assert row["reasons"] == ""
    assert row["response"]


def test_the_newest_is_first(db):
    for i in range(3):
        _record(cardholder_id=str(i))

    assert [r["cardholder_id"] for r in failed_saves.recent()] == ["2", "1", "0"]


def test_only_the_last_few_are_kept(db):
    """Rows are whole forms and whole pages. The next refusal matters more
    than the twenty-first did."""
    for i in range(failed_saves.MAX_ROWS + 5):
        _record(cardholder_id=str(i))

    rows = failed_saves.recent(limit=100)
    assert len(rows) == failed_saves.MAX_ROWS
    assert rows[0]["cardholder_id"] == str(failed_saves.MAX_ROWS + 4)


def test_a_huge_page_is_truncated(db):
    _record(response="x" * (failed_saves.MAX_BODY + 5000))

    (row,) = failed_saves.recent()
    assert "truncated" in row["response"]
    assert len(row["response"]) < failed_saves.MAX_BODY + 200


def test_they_can_be_cleared(db):
    _record()
    _record()

    assert failed_saves.clear() == 2
    assert failed_saves.recent() == []


# =====================================================================
# What is not kept
# =====================================================================


def test_the_token_is_scrubbed_from_the_response(db):
    _record(response=(
        '<input name="__RequestVerificationToken" type="hidden" '
        'value="CfDJ8McWh2dlWelAt4VcjXaYKz0PXWC9DcvUli" />'
    ))

    (row,) = failed_saves.recent()
    assert "CfDJ8McWh2dlWelAt4VcjXaYKz0PXWC9DcvUli" not in row["response"]
    assert "[redacted]" in row["response"]


def test_the_token_is_scrubbed_from_the_request(db):
    _record(request=(
        'Content-Disposition: form-data; name="__RequestVerificationToken"'
        "\r\n\r\nCfDJ8LIVE-TOKEN-VALUE\r\n"
    ))

    (row,) = failed_saves.recent()
    assert "CfDJ8LIVE-TOKEN-VALUE" not in row["request"]


def test_recording_never_raises(monkeypatch):
    """It runs on the failure path, where something is already wrong."""
    monkeypatch.setattr(
        "agsync.failed_saves.get_db",
        lambda: (_ for _ in ()).throw(RuntimeError("no database")),
    )

    _record()  # must not raise


def test_reading_never_raises(monkeypatch):
    monkeypatch.setattr(
        "agsync.failed_saves.get_db",
        lambda: (_ for _ in ()).throw(RuntimeError("no database")),
    )

    assert failed_saves.recent() == []


# =====================================================================
# The client records one
# =====================================================================


def test_a_refused_save_is_recorded_by_the_client(db, millennium_page):
    from agsync.lib.pacs.millennium_ultra.client import MillenniumUltraClient
    from agsync.lib.pacs.millennium_ultra.html_form import CardholderForm

    refusal = millennium_page.replace(
        '<div id="tabHolder">',
        '<div class="field-validation-error">no good</div><div id="tabHolder">', 1,
    )
    client = MillenniumUltraClient(
        base_url="https://hosted105.mgiaccess.test", auth_cookie="session"
    )
    client._http = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=refusal))
    )

    assert client.save_cardholder("12247", CardholderForm.parse(millennium_page)) is False

    (row,) = failed_saves.recent()
    assert row["cardholder_id"] == "12247"
    assert row["vendor"] == "millennium_ultra"
    assert "no good" in row["response"]
    assert "EmployeeID" in row["request"], "the form we sent"


def test_an_http_error_is_recorded_too(db, millennium_page):
    """A 500 has no validation message by definition, so the exchange is
    the only thing there is to look at."""
    from agsync.lib.pacs.millennium_ultra.client import (
        MillenniumError,
        MillenniumUltraClient,
    )
    from agsync.lib.pacs.millennium_ultra.html_form import CardholderForm

    client = MillenniumUltraClient(
        base_url="https://hosted105.mgiaccess.test", auth_cookie="session"
    )
    client._http = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom"))
    )

    with pytest.raises(MillenniumError):
        client.save_cardholder("12247", CardholderForm.parse(millennium_page))

    (row,) = failed_saves.recent()
    assert row["status_code"] == 500
    assert "boom" in row["response"]


def test_a_successful_save_records_nothing(db, millennium_page):
    from agsync.lib.pacs.millennium_ultra.client import MillenniumUltraClient
    from agsync.lib.pacs.millennium_ultra.html_form import CardholderForm

    client = MillenniumUltraClient(
        base_url="https://hosted105.mgiaccess.test", auth_cookie="session"
    )
    client._http = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(302, headers={"Location": "/Cardholders"})
        )
    )

    assert client.save_cardholder("12247", CardholderForm.parse(millennium_page)) is True
    assert failed_saves.recent() == []


# =====================================================================
# The tab
# =====================================================================


def test_the_page_lists_them(db):
    from agsync.routes import failed_saves as route

    _record()
    captured = {}

    class _App:
        class state:
            @staticmethod
            def template_response(request, template, context):
                captured.update(template=template, context=context)
                return "rendered"

    class _Request:
        app = _App()

    assert route.failed_saves_page(request=_Request(), _user=None) == "rendered"
    assert captured["template"] == "failed_saves.html"
    assert captured["context"]["rows"][0]["cardholder_id"] == "12247"


def test_the_tab_is_in_the_navigation():
    from pathlib import Path

    base = Path("src/agsync/templates/base.html").read_text(encoding="utf-8")
    assert 'href="/failed-saves"' in base


def test_every_string_the_page_uses_is_translated():
    """A missing key renders as the key, which looks like a bug."""
    import json
    import re
    from pathlib import Path

    page = Path("src/agsync/templates/failed_saves.html").read_text(encoding="utf-8")
    # Keys only — the template also calls .split('\n'), which is not one.
    used = set(re.findall(r"t\('([a-z0-9_.]+)'\)", page))
    used.add("nav.failed_saves")

    for lang in ("en", "es"):
        strings = json.loads(
            Path(f"src/agsync/locales/{lang}.json").read_text(encoding="utf-8")
        )
        missing = sorted(used - set(strings))
        assert missing == [], f"{lang} is missing {missing}"
