"""The read-only state dump, and the three things it must never do.

Its output is meant to be pasted into a chat or a ticket, and it runs against
a live install, so the dangerous failures are not "it printed the wrong
number" — they are leaking a credential, writing to a database somebody is
using, or generating an encryption key that makes the real one look wrong.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from cryptography.fernet import Fernet

from agsync import diagnostics
from agsync.db.schema import apply_migrations

SECRET_KEY = "papers-motivation-3af25f4e-not-a-real-key"
COOKIE = "AspNet-UltraAuth-cookie-value-that-must-not-be-printed"


@pytest.fixture
def key() -> str:
    return Fernet.generate_key().decode()


@pytest.fixture
def install(tmp_path, key, monkeypatch):
    """A database shaped like a real install, with secrets in it."""
    path = tmp_path / "app.db"
    conn = sqlite3.connect(path)
    apply_migrations(conn)

    def enc(payload) -> str:
        return Fernet(key.encode()).encrypt(json.dumps(payload).encode()).decode()

    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        ("accessgrid", enc({"account_id": "a1ac7ad80", "secret_key": SECRET_KEY})),
    )
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        ("pacs_session:millennium_ultra", enc({"auth_cookie": COOKIE})),
    )
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        ("millennium_seos_slots", enc({
            "11591:seos": [{"slot": 2, "card_number": "1255", "facility_code": "2"}],
            "9146:seos": [{"slot": 3, "card_number": "1299", "facility_code": "2"}],
        })),
    )
    conn.execute(
        """
        INSERT INTO ag_credentials (
            pacs_person_id, pacs_credential_id, ag_card_id, full_name,
            employee_id, status, last_synced_email, last_synced_phone,
            retry_count, created_at, updated_at
        ) VALUES
          ('11591','seos','pn2yUqhdO5Xn_4U','Auston Bunsen','11591','active',
           'ab@accessgrid.com','9546703289',0,'2026-10-01','2026-10-01'),
          ('9146','seos',NULL,'Geraldine Martinez','9146','deleted',
           '','',0,'2026-10-01','2026-10-01')
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("AG_SYNC_DB_PATH", str(path))
    monkeypatch.setenv("AG_SYNC_ENCRYPTION_KEY", key)
    return path


# =====================================================================
# What it must never do
# =====================================================================


def test_no_secret_reaches_the_output(install):
    text = diagnostics.report()

    assert SECRET_KEY not in text
    assert COOKIE not in text
    # And it says so, rather than looking complete.
    assert "accessgrid  [holds credentials — not printed]" in text
    assert "pacs_session:millennium_ultra  [holds credentials — not printed]" in text


def test_contact_details_are_left_out(install):
    """Names are kept — correlating rows with cardholders is the point."""
    text = diagnostics.report()

    assert "Auston Bunsen" in text
    assert "ab@accessgrid.com" not in text
    assert "9546703289" not in text


def test_the_database_cannot_be_written(install):
    conn = diagnostics.open_readonly(install)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("UPDATE ag_credentials SET status = 'wrecked'")


def test_it_does_not_run_migrations(install, monkeypatch):
    """Reading a live install must not upgrade it under the running service."""
    called = []
    monkeypatch.setattr(
        "agsync.db.schema.apply_migrations",
        lambda conn: called.append(conn),
    )
    diagnostics.report()

    assert called == []


def test_it_never_creates_an_encryption_key(tmp_path, monkeypatch):
    """The app generates one on first run; a diagnostic must not."""
    monkeypatch.setattr(diagnostics, "_data_dir", lambda: tmp_path)
    monkeypatch.delenv("AG_SYNC_ENCRYPTION_KEY", raising=False)

    assert diagnostics.resolve_key() is None
    assert list(tmp_path.iterdir()) == []


# =====================================================================
# What it is for
# =====================================================================


def test_it_prints_the_tracking_rows_and_the_ledger(install):
    text = diagnostics.report()

    assert "11591/seos" in text
    assert "pn2yUqhdO5Xn_4U" in text
    assert "11591:seos  ->  2/1255@slot2" in text


def test_it_names_a_deleted_row_that_still_claims_cards(install):
    """The stranded-ledger shape, which no log line reports."""
    text = diagnostics.report()

    assert "9146:seos is marked deleted but still has cards recorded" in text


def test_a_missing_key_is_reported_rather_than_guessed(install, monkeypatch):
    monkeypatch.delenv("AG_SYNC_ENCRYPTION_KEY")
    monkeypatch.setattr(diagnostics, "_data_dir", lambda: install.parent / "empty")
    text = diagnostics.report()

    assert "NOT FOUND" in text
    assert "no encryption key was found" in text


def test_a_missing_database_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv("AG_SYNC_DB_PATH", str(tmp_path / "nothing.db"))
    text = diagnostics.report()

    assert "No database at that path" in text
