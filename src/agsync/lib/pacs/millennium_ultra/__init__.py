"""Millennium Ultra (MGI) web-UI adapter."""

from ..base import ConnectionField, PacsDescriptor
from ..registry import register
from .adapter import MillenniumUltraAdapter

DESCRIPTOR = PacsDescriptor(
    vendor="millennium_ultra",
    display_name="Millennium Ultra (MGI)",
    trigger_help_key="pacs.millennium_ultra.trigger_help",
    connection_fields=[
        ConnectionField(
            "base_url",
            "pacs.millennium_ultra.base_url",
            kind="url",
            placeholder="https://hosted8.mgiaccess.com",
        ),
        ConnectionField(
            "email_domain",
            "pacs.millennium_ultra.email_domain",
            placeholder="cards.example.com",
        ),
        ConnectionField(
            "notify_email",
            "pacs.millennium_ultra.notify_email",
            placeholder="security@example.com",
        ),
    ],
    # Millennium's login is captcha-gated, so the operator hands us a
    # session through the AG Connect side-car before anything else works —
    # including reading the card-format list the trigger is chosen from.
    requires_connect=True,
)

register("millennium_ultra", DESCRIPTOR, lambda params: MillenniumUltraAdapter(**params))

__all__ = ["MillenniumUltraAdapter", "DESCRIPTOR"]
