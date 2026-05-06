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
)

register("avigilon", DESCRIPTOR, lambda params: AvigilonAdapter(**params))

__all__ = ["AvigilonAdapter", "DESCRIPTOR"]
