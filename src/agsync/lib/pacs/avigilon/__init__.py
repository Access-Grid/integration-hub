"""Avigilon Unity (Plasec) adapter."""

from ..base import ConnectionField, PacsDescriptor
from ..registry import register
from .adapter import AvigilonAdapter

DESCRIPTOR = PacsDescriptor(
    vendor="avigilon",
    display_name="Avigilon Unity / Plasec",
    trigger_help_key="pacs.avigilon.trigger_help",
    connection_fields=[
        ConnectionField("host", "pacs.avigilon.host", placeholder="10.0.0.1"),
        ConnectionField("username", "pacs.avigilon.username"),
        ConnectionField("password", "pacs.avigilon.password", kind="password"),
    ],
    # DESFire/SmartTap or Seos, from the AccessGrid template: Seos has the
    # hub create a token holding the card number AccessGrid allocated.
    derives_mode_from_template=True,
    # Enrollment is one word on a token, so batches arrive large.
    max_provisions_per_cycle=200,
    # A token with no end date is meant to last; a year would lapse unseen.
    default_pass_days=3650,
    # A token names no card format, so the site's is chosen once in setup.
    picks_card_format=True,
)

register("avigilon", DESCRIPTOR, lambda params: AvigilonAdapter(**params))

__all__ = ["AvigilonAdapter", "DESCRIPTOR"]
