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
        # The session cookie a Millennium login produces. Named `Ultra`
        # since the move to ASP.NET Core; it was `.AspNet.UltraAuth` on the
        # classic build, which no install still runs.
        required_cookie="Ultra",
        # Companions its screens expect alongside it. `timeoffset` is the
        # one that matters: the date fields are rendered and parsed against
        # it, so a session without it writes activation times in the wrong
        # timezone.
        # The anti-forgery cookie. Only the bulk-export endpoints check it,
        # and without it they answer 502 rather than 403 — which reads as
        # the server being unwell rather than as a rejected request, and
        # cost an afternoon to pin down.
        #
        # ASP.NET Core suffixes its name per application
        # (".AspNetCore.Antiforgery._1OIVLT0AFo" on one install), so it is
        # matched by prefix. A trailing "*" means prefix rather than exact.
        # Its name therefore travels with the session, since no constant
        # can name it.
        #
        # Note this is the anti-forgery *cookie*. The hidden form field of
        # the same job is still called __RequestVerificationToken on Core,
        # and is read from the page rather than from the jar.
        # Everything the signed-in browser holds for this origin.
        # DisableAlarmSound looks like a console preference and probably is
        # one, but the cost of carrying a cookie we do not need is a few
        # bytes, and the cost of omitting one we do need is a 502 that
        # reads as the server being unwell. The second kind has already
        # cost an afternoon once.
        #
        # Still an allowlist rather than "whatever is in the jar": this
        # build offers SSO at /Account/SsoLogin, and a login that goes
        # through an identity provider would leave that provider's own
        # session cookies in the same jar. Those are not ours to store.
        extra_cookies=(
            "UltraCoreCompanyName",
            "timeoffset",
            "DisableAlarmSound",
            ".AspNetCore.Antiforgery.*",
        ),
        # /Account/LogIn redirects here on the Core build.
        login_path="/Account",
    ),
    # DESFire vs Seos is decided by the AccessGrid template's protocol, not
    # by the operator — asking would let the two disagree, and a wrong
    # answer either writes cards into the PACS that should not exist or
    # never provisions at all.
    derives_mode_from_template=True,
)

register("millennium_ultra", DESCRIPTOR, lambda params: MillenniumUltraAdapter(**params))

__all__ = ["MillenniumUltraAdapter", "DESCRIPTOR"]
