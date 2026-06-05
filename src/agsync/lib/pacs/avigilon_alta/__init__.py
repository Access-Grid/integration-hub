"""Avigilon Alta Access (cloud / OpenPath Helium) adapter."""

from ..base import ConnectionField, PacsDescriptor
from ..registry import register
from .adapter import AvigilonAltaAdapter

DESCRIPTOR = PacsDescriptor(
    vendor="avigilon_alta",
    display_name="Avigilon Alta Access (Cloud)",
    trigger_help_key="pacs.avigilon_alta.trigger_help",
    connection_fields=[
        ConnectionField("email", "pacs.avigilon_alta.email", placeholder="you@example.com"),
        ConnectionField("password", "pacs.avigilon_alta.password", kind="password"),
    ],
)

register("avigilon_alta", DESCRIPTOR, lambda params: AvigilonAltaAdapter(**params))

__all__ = ["AvigilonAltaAdapter", "DESCRIPTOR"]
