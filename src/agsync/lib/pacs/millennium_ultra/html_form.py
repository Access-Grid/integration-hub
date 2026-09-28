"""Generic HTML form parse → mutate → re-post, for Millennium Ultra.

Millennium's cardholder screen has no write API. The only way to change a
card is to POST the whole cardholder form back to
``/Cardholders/Cardholders/Index/<id>``, and the server treats that POST as
the complete new state of the record: **every field we fail to echo back is
wiped**. A hand-written field list would silently drop anything this install
has that the capture didn't (custom user fields, a tenant column, a future
Millennium build's new input), so we don't write one. We parse *every* named
control out of the served HTML and replay all of them, changing only the
handful of keys the sync engine actually owns.

The replay reproduces browser submit semantics exactly, because that is the
only behaviour the server is known to accept:

  * document order is preserved (verified byte-for-byte against a captured
    browser POST in the tests)
  * unchecked checkboxes and unselected radios are omitted entirely — that
    absence *is* the "off" value, and it's how a card gets deactivated
    (``Card_N_Active``)
  * a ``<select>`` with no ``selected`` option submits its first option,
    which is what the browser does and what an empty card slot relies on
  * file inputs (photoImage/signImage) are replayed as empty file parts.
    An empty part is a no-op — the captured save proves an existing photo
    survives it — whereas dropping the part entirely is untested.
  * ``disabled`` is deliberately ignored: this app's markup mis-renders the
    attribute into ``class`` and its own JS re-enables fields before submit,
    so the browser posts them anyway.

Values are HTML-unescaped on parse (``Card_N_AccessLevels`` holds escaped
JSON) and sent raw, matching the wire bytes of a real submit.
"""

from __future__ import annotations

import random
import string
from dataclasses import dataclass, field
from html.parser import HTMLParser

# Control types a browser never submits, even when named.
_NON_SUBMITTING = frozenset({"submit", "button", "reset", "image"})


@dataclass
class Option:
    value: str
    selected: bool = False
    label: str = ""


@dataclass
class Control:
    name: str
    kind: str  # text | hidden | checkbox | radio | select | file | textarea | ...
    value: str = ""
    checked: bool = False
    options: list[Option] = field(default_factory=list)

    @property
    def submits(self) -> bool:
        if self.kind in _NON_SUBMITTING:
            return False
        if self.kind in ("checkbox", "radio"):
            return self.checked
        return True

    @property
    def submitted_value(self) -> str:
        if self.kind == "select":
            for opt in self.options:
                if opt.selected:
                    return opt.value
            # No explicit selection: browsers fall back to the first option.
            return self.options[0].value if self.options else ""
        return self.value


class _FormParser(HTMLParser):
    """Collect named controls from one <form>, in document order."""

    def __init__(self, form_id: str | None, action_contains: str | None):
        super().__init__(convert_charrefs=True)
        self._form_id = form_id
        self._action_contains = action_contains
        self._in_form = False
        self._done = False
        self.action = ""
        self.controls: list[Control] = []
        self._select: Control | None = None
        self._textarea: Control | None = None
        self._option: Option | None = None

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _attrs(pairs: list[tuple[str, str | None]]) -> dict[str, str]:
        # A bare attribute (checked, selected, multiple) parses as None; map
        # it to its own name so presence checks are uniform.
        return {k.lower(): (v if v is not None else k) for k, v in pairs}

    def _wanted(self, attrs: dict[str, str]) -> bool:
        if self._form_id and attrs.get("id") == self._form_id:
            return True
        if self._action_contains and self._action_contains in attrs.get("action", ""):
            return True
        return not self._form_id and not self._action_contains

    # -- HTMLParser hooks ------------------------------------------------

    def handle_starttag(self, tag: str, attrlist: list[tuple[str, str | None]]) -> None:
        if self._done:
            return
        attrs = self._attrs(attrlist)
        tag = tag.lower()

        if tag == "form":
            if not self._in_form and self._wanted(attrs):
                self._in_form = True
                self.action = attrs.get("action", "")
            return

        if not self._in_form:
            return

        name = attrs.get("name", "")

        if tag == "input":
            if not name:
                return
            kind = (attrs.get("type") or "text").lower()
            self.controls.append(
                Control(
                    name=name,
                    kind=kind,
                    value=attrs.get("value", ""),
                    checked="checked" in attrs,
                )
            )
        elif tag == "select":
            self._select = Control(name=name, kind="select") if name else None
            if self._select is not None:
                self.controls.append(self._select)
        elif tag == "option" and self._select is not None:
            self._option = Option(
                value=attrs.get("value", ""), selected="selected" in attrs
            )
            self._select.options.append(self._option)
        elif tag == "textarea":
            self._textarea = Control(name=name, kind="textarea") if name else None
            if self._textarea is not None:
                self.controls.append(self._textarea)

    def handle_endtag(self, tag: str) -> None:
        if self._done:
            return
        tag = tag.lower()
        if tag == "form" and self._in_form:
            self._done = True
        elif tag == "select":
            self._select = None
            self._option = None
        elif tag == "option":
            self._option = None
        elif tag == "textarea":
            self._textarea = None

    def handle_data(self, data: str) -> None:
        if self._done or not self._in_form:
            return
        if self._option is not None:
            self._option.label += data
        elif self._textarea is not None:
            self._textarea.value += data


class CardholderForm:
    """A parsed cardholder form, ready to mutate and re-post."""

    def __init__(self, action: str, controls: list[Control]):
        self.action = action
        self.controls = controls

    # -- construction ----------------------------------------------------

    @classmethod
    def parse(
        cls,
        html: str,
        form_id: str = "myForm",
        action_contains: str = "/Cardholders/Cardholders/Index/",
    ) -> CardholderForm:
        parser = _FormParser(form_id, action_contains)
        parser.feed(html)
        parser.close()
        if not parser.controls:
            raise ValueError("No form controls found — not a cardholder page?")
        return cls(parser.action, parser.controls)

    # -- reads -----------------------------------------------------------

    def find(self, name: str) -> Control | None:
        for control in self.controls:
            if control.name == name:
                return control
        return None

    def value(self, name: str, default: str = "") -> str:
        control = self.find(name)
        return control.submitted_value if control is not None else default

    def is_checked(self, name: str) -> bool:
        control = self.find(name)
        return bool(control and control.checked)

    def options(self, name: str) -> list[Option]:
        control = self.find(name)
        return list(control.options) if control else []

    @property
    def token(self) -> str:
        """The per-page anti-forgery token, which differs from the cookie one."""
        return self.value("__RequestVerificationToken")

    # -- writes ----------------------------------------------------------

    def set_value(self, name: str, value: str) -> None:
        """Set a text/hidden field, or pick a <select> option by value.

        Raises KeyError for an unknown field: every write this integration
        performs targets a field we have already read off the page, so a miss
        means the page shape changed and we must not guess.
        """
        control = self.find(name)
        if control is None:
            raise KeyError(name)
        if control.kind == "select":
            match = next((o for o in control.options if o.value == value), None)
            if match is None:
                raise KeyError(f"{name} has no option {value!r}")
            for opt in control.options:
                opt.selected = opt is match
        else:
            control.value = value

    def set_checked(self, name: str, checked: bool) -> None:
        control = self.find(name)
        if control is None:
            raise KeyError(name)
        control.checked = bool(checked)

    # -- serialization ---------------------------------------------------

    def to_multipart(self, boundary: str | None = None) -> tuple[str, bytes]:
        """Render the form as a browser would submit it.

        Returns (content_type, body). Multipart rather than urlencoded
        because the form declares enctype="multipart/form-data" and carries
        two file inputs.
        """
        boundary = boundary or _random_boundary()
        marker = f"--{boundary}".encode()
        chunks: list[bytes] = []
        for control in self.controls:
            if not control.submits:
                continue
            chunks.append(marker + b"\r\n")
            if control.kind == "file":
                chunks.append(
                    f'Content-Disposition: form-data; name="{control.name}"; filename=""'
                    "\r\nContent-Type: application/octet-stream\r\n\r\n".encode()
                )
                chunks.append(b"\r\n")
            else:
                chunks.append(
                    f'Content-Disposition: form-data; name="{control.name}"\r\n\r\n'.encode()
                )
                chunks.append(control.submitted_value.encode("utf-8") + b"\r\n")
        chunks.append(marker + b"--\r\n")
        return f"multipart/form-data; boundary={boundary}", b"".join(chunks)


def _random_boundary() -> str:
    alphabet = string.ascii_letters + string.digits
    return "----WebKitFormBoundary" + "".join(random.choices(alphabet, k=16))
