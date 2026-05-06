"""Single-admin user storage.

Only one row ever exists in the `admin` table. The wizard creates it,
the CLI `agsync reset-admin` deletes it. Passwords are hashed with
argon2id; we never store the plaintext.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from ..db.connection import execute_one, get_db

_hasher = PasswordHasher()


def admin_exists() -> bool:
    row = execute_one("SELECT 1 FROM admin WHERE id = 1")
    return row is not None


def create_admin(username: str, password: str) -> None:
    pw_hash = _hasher.hash(password)
    get_db().execute(
        "INSERT INTO admin (id, username, password_hash) VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET username = excluded.username, password_hash = excluded.password_hash",
        (username, pw_hash),
    )


def delete_admin() -> None:
    get_db().execute("DELETE FROM admin WHERE id = 1")


def verify_admin_password(username: str, password: str) -> bool:
    row = execute_one("SELECT username, password_hash FROM admin WHERE id = 1")
    if not row:
        return False
    if row["username"] != username:
        return False
    try:
        _hasher.verify(row["password_hash"], password)
    except VerifyMismatchError:
        return False
    # Optionally upgrade hash if the parameters changed.
    if _hasher.check_needs_rehash(row["password_hash"]):
        get_db().execute(
            "UPDATE admin SET password_hash = ? WHERE id = 1",
            (_hasher.hash(password),),
        )
    return True


def get_admin_username() -> str | None:
    row = execute_one("SELECT username FROM admin WHERE id = 1")
    return row["username"] if row else None
