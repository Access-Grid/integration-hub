"""AccessGrid API access — wraps the `accessgrid` PyPI SDK.

We re-export `AccessGrid` and `AccessGridError` so callers can `from
agsync.ag import AccessGrid` without caring whether it comes from the
SDK or some local override. We also provide a `test_connection`
helper that tries a cheap, idempotent call and returns a tuple
(ok, error_message) instead of raising — matching the PacsAdapter shape.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from accessgrid import AccessGrid, AccessGridError

logger = logging.getLogger(__name__)

__all__ = [
    "AccessGrid",
    "AccessGridError",
    "TemplateShape",
    "build_client",
    "read_template_shape",
    "template_protocol",
    "test_connection",
]

# Credential technology of a card template, as AccessGrid reports it. Seos
# is the one that changes behaviour: AccessGrid mints the credential and the
# PACS receives it, rather than the other way round.
PROTOCOL_SEOS = "seos"


def build_client(account_id: str, secret_key: str) -> AccessGrid:
    return AccessGrid(account_id=account_id, secret_key=secret_key)


@dataclass(frozen=True)
class TemplateShape:
    """What a configured card template is, before anything is issued from it.

    Both facts here change how a credential has to be written into a PACS,
    and both were previously only discoverable *after* provisioning — by
    looking at what came back. AccessGrid states them up front:
    `GET /v1/console/card-templates/{id}` answers with a `templates` list
    for a pair and a single template otherwise.

    Why each matters, for a PACS that receives credentials:

    * **paired** — one issue produces two passes, one per platform. On HID
      that means two card numbers and two of the cardholder's three slots;
      the halves are only findable afterwards through the reference we
      attach, because the combined identifier never appears in a listing.
    * **protocol** — on DESFire and SmartTap both halves of a pair share one
      card number, so a pair consumes one slot rather than two. Reading the
      pair without the technology would allocate one slot too many.

    `known` is False when the template could not be read at all, which
    callers should treat as "do not branch on this" rather than as a value.
    """

    protocol: str = ""
    paired: bool = False
    platforms: tuple[str, ...] = ()
    known: bool = False

    @property
    def slots_per_issue(self) -> int:
        """How many PACS card slots one issue of this template needs.

        One unless the template is a pair whose halves carry different card
        numbers. Falls back to one when the shape is unknown, because
        over-reserving a slot on a cardholder who has three stops a pass
        being issued at all.
        """
        if not self.known or not self.paired:
            return 1
        return 1 if self.protocol in _SHARED_IDENTITY_PROTOCOLS else 2


# Technologies where both halves of a template pair carry the same card
# number, so the PACS only ever holds one card for the pair.
_SHARED_IDENTITY_PROTOCOLS = frozenset({"desfire", "smarttap", "smart_tap"})


def read_template_shape(client: AccessGrid, template_id: str) -> TemplateShape:
    """Read a card template, or report that it could not be read.

    Never raises: a sync cycle must not stop because a template lookup did.
    """
    try:
        result = client.console.read_template(template_id)
    except Exception as e:  # noqa: BLE001 — never let this break a sync cycle
        logger.warning("Could not read card template %s: %s", template_id, e)
        return TemplateShape()

    # The SDK returns a list only when the response carried a `templates`
    # key, which is exactly how a pair is reported.
    paired = isinstance(result, list)
    templates = result if paired else [result]
    protocol = ""
    platforms = []
    for template in templates:
        found = (getattr(template, "protocol", "") or "").strip().lower()
        if found and not protocol:
            protocol = found
        platform = (getattr(template, "platform", "") or "").strip().lower()
        if platform:
            platforms.append(platform)
    return TemplateShape(
        protocol=protocol,
        paired=paired and len(templates) > 1,
        platforms=tuple(platforms),
        known=True,
    )


def template_protocol(client: AccessGrid, template_id: str) -> str:
    """The credential technology a card template issues, e.g. "seos".

    A template id can name a pair, in which case both halves carry the same
    protocol and either will do. Returns "" when it cannot be determined,
    which callers should treat as "assume the read-only direction" rather
    than guessing.
    """
    return read_template_shape(client, template_id).protocol


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
