"""AccessGrid API access — wraps the `accessgrid` PyPI SDK.

We re-export `AccessGrid` and `AccessGridError` so callers can `from
agsync.ag import AccessGrid` without caring whether it comes from the
SDK or some local override. We also provide a `test_connection`
helper that tries a cheap, idempotent call and returns a tuple
(ok, error_message) instead of raising — matching the PacsAdapter shape.
"""

from __future__ import annotations

from accessgrid import AccessGrid, AccessGridError

__all__ = ["AccessGrid", "AccessGridError", "build_client", "test_connection"]


def build_client(account_id: str, secret_key: str) -> AccessGrid:
    return AccessGrid(account_id=account_id, secret_key=secret_key)


def test_connection(account_id: str, secret_key: str, template_id: str) -> tuple[bool, str]:
    """Smoke-test by listing cards under the configured template.

    A successful list (even of 0 cards) means auth works and the
    template id is valid for this account.
    """
    try:
        client = build_client(account_id, secret_key)
        # Both signatures get accepted by the SDK; some versions take
        # template_id positionally, others as kwarg.
        client.access_cards.list(template_id=template_id)
        return True, ""
    except AccessGridError as e:
        return False, f"AccessGrid: {e}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
