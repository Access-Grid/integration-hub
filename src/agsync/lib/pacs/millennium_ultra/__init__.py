"""Millennium Ultra (MGI Access) adapter — hosted web interface."""

from ..base import ConnectionField, PacsDescriptor
from ..registry import register
from .adapter import MillenniumUltraAdapter

DESCRIPTOR = PacsDescriptor(
    vendor="millennium_ultra",
    display_name="Millennium Ultra (MGI Access)",
    trigger_help_key="pacs.millennium_ultra.trigger_help",
    connection_fields=[
        ConnectionField(
            "base_url",
            "pacs.millennium_ultra.base_url",
            kind="url",
            placeholder="https://hosted8.mgiaccess.com",
        ),
        # The login is captcha-protected, so a human signs in and we adopt the
        # resulting cookie; no password is ever collected or stored. A guided
        # "open login window" flow replaces this paste box later.
        ConnectionField(
            "session",
            "pacs.millennium_ultra.session",
            placeholder="curl 'https://…' -b '…' or the .AspNet.UltraAuth value",
        ),
        # Per-tenant numeric id. The config screen grows a discovered dropdown
        # (adapter.card_formats()) once the wizard supports a second stage.
        ConnectionField(
            "card_format",
            "pacs.millennium_ultra.card_format",
            placeholder="7",
        ),
        ConnectionField(
            "email_domain",
            "pacs.millennium_ultra.email_domain",
            placeholder="iconcreds.com",
        ),
    ],
    # Facility code and card number are separate decimal fields already, so
    # there is no pre-encoded payload to send verbatim.
    supports_file_data=False,
)

register("millennium_ultra", DESCRIPTOR, lambda params: MillenniumUltraAdapter(**params))

__all__ = ["MillenniumUltraAdapter", "DESCRIPTOR"]
