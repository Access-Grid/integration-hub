"""AccessGrid API access — wraps the `accessgrid` PyPI SDK.

We re-export `AccessGrid` and `AccessGridError` so callers can `from
agsync.ag import AccessGrid` without caring whether it comes from the
SDK or some local override. We also provide a `test_connection`
helper that tries a cheap, idempotent call and returns a tuple
(ok, error_message) instead of raising — matching the PacsAdapter shape.
"""

from __future__ import annotations

import logging

from accessgrid import AccessGrid, AccessGridError

logger = logging.getLogger(__name__)

__all__ = [
    "AccessGrid",
    "AccessGridError",
    "build_client",
    "template_protocol",
    "test_connection",
]

# Credential technology of a card template, as AccessGrid reports it. Seos
# is the one that changes behaviour: AccessGrid mints the credential and the
# PACS receives it, rather than the other way round.
PROTOCOL_SEOS = "seos"


def build_client(account_id: str, secret_key: str) -> AccessGrid:
    return AccessGrid(account_id=account_id, secret_key=secret_key)


def template_protocol(client: AccessGrid, template_id: str) -> str:
    """The credential technology a card template issues, e.g. "seos".

    A template id can name a pair — one Apple template, one Android — and
    the endpoint answers those with a list instead of a single template.
    Every half that declares a protocol has to agree before this reports
    one: the protocol decides which direction the whole integration runs
    in, and a pair whose halves disagree has no single answer.

    Returns "" when it cannot be determined, which callers should treat as
    "assume the read-only direction" rather than guessing. Reading it as
    the writing direction would have us create credentials in a PACS on the
    strength of a value we never established.
    """
    try:
        result = client.console.read_template(template_id)
    except Exception as e:  # noqa: BLE001 — never let this break a sync cycle
        logger.warning("Could not read card template %s: %s", template_id, e)
        return ""

    templates = result if isinstance(result, list) else [result]
    protocols = {
        (getattr(t, "protocol", "") or "").strip().lower() for t in templates
    }
    # A half that declares nothing is not a vote for anything; the halves
    # that do declare still have to agree.
    protocols.discard("")

    if len(protocols) == 1:
        return protocols.pop()
    if protocols:
        logger.warning(
            "Card template %s is a pair whose halves disagree about protocol "
            "(%s) — treating it as undetermined",
            template_id, ", ".join(sorted(protocols)),
        )
    return ""


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
