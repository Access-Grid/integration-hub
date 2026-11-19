"""The logs page's Download button: the newest 5,000 entries as logs.txt."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from agsync.auth import require_admin
from agsync.logs import store


@pytest.fixture
def client(monkeypatch):
    from agsync.server import create_app

    app = create_app()
    app.dependency_overrides[require_admin] = lambda: {"username": "admin"}
    with TestClient(app) as c:
        yield c


def test_download_is_the_newest_5000_entries_oldest_first_as_logs_txt(client):
    for i in range(5_010):
        store.log("INFO", f"entry {i}", phase="phase2" if i % 2 else None)
    resp = client.get("/logs/download")
    assert resp.status_code == 200
    assert resp.headers["content-disposition"] == 'attachment; filename="logs.txt"'
    assert resp.headers["content-type"].startswith("text/plain")
    lines = resp.text.splitlines()
    mine = [line for line in lines if " entry " in line]
    assert len(mine) <= 5_000 and mine[-1].endswith("entry 5009")
    assert len(lines) == 5_000, "exactly the newest 5,000 entries"
    assert lines[-1].split(" ", 1)[1] == "INFO    [phase2] entry 5009"
    numbers = [int(line.rsplit(" ", 1)[1]) for line in mine]
    assert numbers == sorted(numbers), "oldest first, as a log file reads"


def test_a_traceback_stays_whole_under_its_entry(client):
    store.install_handler()
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("agsync.test").error("it failed", exc_info=True)
    text = client.get("/logs/download").text
    tail = text[text.index("ERROR   it failed"):]
    assert "Traceback (most recent call last)" in tail and "ValueError: boom" in tail


def test_the_logs_page_has_the_download_button(client):
    page = client.get("/logs").text
    assert 'href="/logs/download"' in page and 'download="logs.txt"' in page
    assert "Download logs" in page


def test_download_needs_an_admin():
    from agsync.server import create_app

    with TestClient(create_app()) as c:
        resp = c.get("/logs/download", follow_redirects=False)
    assert resp.status_code in (302, 303, 401)
    assert "entry" not in resp.text
