"""Millennium Ultra (MGI) web-UI adapter."""

from ..base import BrowserLogin, ConnectionField, PacsDescriptor
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
    browser_login=BrowserLogin(
        # The forms-auth cookie a Millennium login produces.
        required_cookie=".AspNet.UltraAuth",
        # Companions its screens expect alongside it. `timeoffset` is the
        # one that matters: the date fields are rendered and parsed against
        # it, so a session without it writes activation times in the wrong
        # timezone.
        # __RequestVerificationToken is ASP.NET's anti-forgery cookie. Only
        # the bulk-export endpoints check it, and without it they answer 502
        # rather than 403 — which reads as the server being unwell rather
        # than as a rejected request, and cost an afternoon to pin down.
        extra_cookies=("UltraCompanyName", "timeoffset", "__RequestVerificationToken"),
        login_path="/Account/LogIn",
    ),
    # DESFire vs Seos is decided by the AccessGrid template's protocol, not
    # by the operator — asking would let the two disagree, and a wrong
    # answer either writes cards into the PACS that should not exist or
    # never provisions at all.
    derives_mode_from_template=True,
)

register("millennium_ultra", DESCRIPTOR, lambda params: MillenniumUltraAdapter(**params))

__all__ = ["MillenniumUltraAdapter", "DESCRIPTOR"]
