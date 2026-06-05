"""Test doubles shared across the Avigilon Alta suite."""

from __future__ import annotations


class FakeAltaClient:
    """Stand-in for AltaClient — no network, records write calls."""

    def __init__(self, users=None, creds_by_user=None):
        self._users = users or []
        self._creds_by_user = creds_by_user or {}
        self.patch_calls: list[dict] = []
        self.patch_result = True

    def list_users(self):
        return self._users

    def list_credentials(self, user_id):
        return self._creds_by_user.get(str(user_id), [])

    def patch_credential_dates(self, user_id, credential_id, **kwargs):
        self.patch_calls.append({"user_id": user_id, "credential_id": credential_id, **kwargs})
        return self.patch_result
