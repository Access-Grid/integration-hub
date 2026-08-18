"""Millennium Ultra → vendor-agnostic adapter.

What makes Millennium Ultra different from the other adapters:

  * **The enrollment trigger is a card's *format*.** Each cardholder has three
    fixed card slots, and a slot is enrolled when its Card Format matches the
    id chosen at config time. Format ids are per-tenant, so they are
    discovered from a live page (see `card_formats`) rather than hardcoded.
    The trigger is per-slot, so one of a cardholder's cards can be enrolled
    without the others.

  * **Card data only exists on the cardholder's HTML page.** The roster
    endpoint is cheap but carries no card fields, so a full sweep costs one
    ~90 KB page per cardholder. Those fetches are the expensive part of a
    cycle and run concurrently.

  * **Nobody has an email address.** On the reference tenant the Personal
    Information module is switched off, so `EMail` and `Phone` are empty and
    rendered disabled for every cardholder. Phase 1 skips anyone with neither,
    which would mean provisioning nothing at all, so addresses are synthesized
    from the configured domain — with collisions resolved, because the roster
    genuinely contains four separate people named "Acebedo, RFID. Jose".

  * **The roster's `IsActive` does not mean what it looks like.** It flags the
    row currently *selected* in the UI list, not whether the cardholder is
    enabled: requesting the list with a different id in the path moves the
    `true` to that row. It is deliberately ignored here. Real enable/disable
    state lives on each card's Active checkbox, which is where
    `Credential.status` comes from.

Facility code and card number are already separate decimal fields, so unlike
CDVI there is nothing to decode and no `file_data` alternative to offer.

Read-only for now: `update_credential_status` is not implemented, so phase 4
skips this vendor. The generic form serializer it needs already exists in
`parse.serialize_form`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from ..base import (
    ConnectionResult,
    Credential,
    CredentialStatus,
    PacsDescriptor,
    Person,
)
from .client import MillenniumUltraClient, SessionExpired, parse_pasted_session
from .parse import Cardholder, assign_emails, parse_card_formats, parse_cardholder

logger = logging.getLogger(__name__)

DEFAULT_EMAIL_DOMAIN = "iconcreds.com"


class MillenniumUltraAdapter:
    def __init__(
        self,
        base_url: str,
        session: str = "",
        card_format: str = "",
        email_domain: str = DEFAULT_EMAIL_DOMAIN,
        **_extra: Any,
    ) -> None:
        self._client = MillenniumUltraClient(
            base_url=base_url,
            session_cookies=parse_pasted_session(session),
        )
        self._card_format = str(card_format or "").strip()
        self._email_domain = (email_domain or DEFAULT_EMAIL_DOMAIN).strip()
        # Populated by list_people(); the engine always calls it first, and
        # rebuilds the adapter each cycle, so this is a per-cycle cache.
        self._cardholders: dict[str, Cardholder] = {}
        self._emails: dict[str, str] = {}

    def descriptor(self) -> PacsDescriptor:
        from . import DESCRIPTOR

        return DESCRIPTOR

    def close(self) -> None:
        self._client.close()

    # -- connection --------------------------------------------------------

    def test_connection(self) -> ConnectionResult:
        ok, message = self._client.test_connection()
        return ConnectionResult(ok=ok, message=message)

    def card_formats(self) -> list[tuple[str, str]]:
        """The tenant's card formats as [(id, label)], for the config screen.

        Any cardholder's page carries the full dropdown, so this reads the
        first one the roster returns.
        """
        roster = self._client.list_roster()
        if not roster:
            return []
        html = self._client.get_cardholder_html(str(roster[0].get("ID")))
        return parse_card_formats(html)

    # -- reads -------------------------------------------------------------

    def list_people(self) -> Iterable[Person]:
        roster = self._client.list_roster()
        ids = [str(row.get("ID")) for row in roster if row.get("ID") is not None]

        # The only source of card data, and of first/last name. One page per
        # cardholder, fetched concurrently.
        logger.info("Millennium Ultra: fetching %d cardholder pages", len(ids))
        pages = self._client.get_cardholders_html(ids)

        self._cardholders = {
            pid: parse_cardholder(html, cardholder_id=pid) for pid, html in pages.items()
        }
        logger.info("Millennium Ultra: parsed %d cardholder pages", len(self._cardholders))

        # Resolve email collisions across the whole roster at once, so an
        # address never silently moves between people as the roster grows.
        self._emails = assign_emails(
            [(c.id, c.first_name, c.last_name) for c in self._cardholders.values()],
            self._email_domain,
        )

        for pid, holder in self._cardholders.items():
            yield Person(
                id=pid,
                full_name=holder.full_name,
                first_name=holder.first_name,
                last_name=holder.last_name,
                # Real addresses win when a tenant does populate them.
                email=holder.email or self._emails.get(pid, ""),
                phone=holder.phone,
                # Millennium Ultra exposes no reliable per-cardholder enable
                # flag here (see the module docstring on IsActive); card-level
                # Active carries the state that matters.
                active=True,
                raw={"employee_id": holder.employee_id},
            )

    def list_credentials(self, person_id: str) -> Iterable[Credential]:
        holder = self._cardholders.get(str(person_id))
        if holder is None:
            return

        for slot in holder.cards:
            if slot.is_empty:
                continue

            trigger = bool(self._card_format) and slot.card_format == self._card_format
            if trigger:
                logger.debug(
                    "Millennium Ultra: cardholder %s slot %d enrolled (format %s)",
                    holder.id, slot.index, slot.card_format,
                )

            yield Credential(
                id=slot.identity,
                person_id=str(person_id),
                card_number=slot.encoded,
                site_code=slot.facility,
                status=(CredentialStatus.ACTIVE if slot.active else CredentialStatus.SUSPENDED),
                activate_date=slot.activation,
                deactivate_date=slot.expiration,
                trigger_active=trigger,
                raw={
                    "slot": slot.index,
                    "card_id": slot.card_id,
                    "card_format": slot.card_format,
                },
            )

    def update_credential_status(
        self, person_id: str, credential_id: str, status: CredentialStatus
    ) -> bool:
        # Not implemented yet — writeback is a read-modify-write that re-posts
        # the entire cardholder form, and a field we failed to reproduce would
        # silently blank real data in a production access control system. It
        # lands behind a dry-run once the read path is proven. Phase 4 skips
        # this vendor while supports_status_writeback is False.
        return False

    @property
    def supports_status_writeback(self) -> bool:
        return False


__all__ = ["MillenniumUltraAdapter", "SessionExpired"]


HELP_TEXT: dict[str, dict[str, str]] = {
    "en": {
        "pacs.millennium_ultra.base_url": "Millennium Ultra URL (e.g. https://hosted8.mgiaccess.com)",
        "pacs.millennium_ultra.session": "Signed-in session (paste a 'Copy as cURL' command or the .AspNet.UltraAuth cookie)",
        "pacs.millennium_ultra.card_format": "Card Format id that marks a card for AccessGrid",
        "pacs.millennium_ultra.email_domain": "Email domain for generated addresses",
        "pacs.millennium_ultra.trigger_help": (
            "A card is enrolled when its Card Format matches the id you enter "
            "below. The trigger is per card, so one of a cardholder's three "
            "card slots can be enrolled without the others. Pick a format "
            "reserved for AccessGrid — choosing one already used by existing "
            "badges would enroll everyone holding that card type. "
            "Millennium Ultra's login is protected by a captcha, so sign in "
            "with your browser and paste the session here: open developer "
            "tools, go to the Network tab, right-click any request and choose "
            "Copy as cURL, then paste the whole thing. Do not use "
            "document.cookie in the console — the session cookie is HttpOnly "
            "and will not appear there. Cardholders on this system have no "
            "email address, so AccessGrid addresses are generated from each "
            "person's name and the domain you set. This integration only "
            "reads from Millennium Ultra; it never creates or changes "
            "cardholders or cards."
        ),
    },
    "es": {
        "pacs.millennium_ultra.base_url": "URL de Millennium Ultra (p. ej. https://hosted8.mgiaccess.com)",
        "pacs.millennium_ultra.session": "Sesión iniciada (pegue un comando 'Copy as cURL' o la cookie .AspNet.UltraAuth)",
        "pacs.millennium_ultra.card_format": "Id del formato de tarjeta que marca una tarjeta para AccessGrid",
        "pacs.millennium_ultra.email_domain": "Dominio de correo para las direcciones generadas",
        "pacs.millennium_ultra.trigger_help": (
            "Una tarjeta se inscribe cuando su Formato de Tarjeta coincide con "
            "el id que introduzca abajo. El disparador es por tarjeta, así que "
            "una de las tres ranuras del titular puede inscribirse sin las "
            "demás. Elija un formato reservado para AccessGrid: usar uno que "
            "ya tengan las credenciales existentes inscribiría a todos los que "
            "las poseen. El inicio de sesión de Millennium Ultra está "
            "protegido por un captcha, así que inicie sesión en su navegador y "
            "pegue aquí la sesión: abra las herramientas de desarrollo, vaya a "
            "la pestaña Red, haga clic derecho en cualquier solicitud y elija "
            "Copy as cURL, y pegue todo el texto. No use document.cookie en la "
            "consola: la cookie de sesión es HttpOnly y no aparecerá allí. Los "
            "titulares de este sistema no tienen correo electrónico, por lo "
            "que las direcciones de AccessGrid se generan a partir del nombre "
            "de cada persona y del dominio que indique. Esta integración solo "
            "lee de Millennium Ultra; nunca crea ni modifica titulares ni "
            "tarjetas."
        ),
    },
}
