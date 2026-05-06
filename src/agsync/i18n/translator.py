"""JSON-dictionary based i18n.

Keys missing from a non-English locale fall back to English so the UI
never shows a raw key. Add a new locale by dropping a JSON file in
src/agsync/locales/<code>.json.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files
from typing import Any

DEFAULT = "en"


@lru_cache(maxsize=8)
def _load(locale: str) -> dict[str, str]:
    try:
        text = (files("agsync.locales") / f"{locale}.json").read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    return json.loads(text)


def available_locales() -> list[str]:
    return ["en", "es"]


def default_locale() -> str:
    return DEFAULT


class Translator:
    def __init__(self, locale: str):
        self.locale = locale if locale in available_locales() else DEFAULT
        self._dict = _load(self.locale)
        self._fallback = _load(DEFAULT) if self.locale != DEFAULT else self._dict

    def t(self, key: str, **kwargs: Any) -> str:
        s = self._dict.get(key) or self._fallback.get(key) or key
        if kwargs:
            try:
                return s.format(**kwargs)
            except (KeyError, IndexError):
                return s
        return s


def get_translator(locale: str | None) -> Translator:
    return Translator(locale or DEFAULT)
