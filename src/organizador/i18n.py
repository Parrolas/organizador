"""Dictionary-based UI translations with Portuguese as the source language.

Source strings are the Portuguese literals themselves: ``_("Novo material")``
returns the active language's translation, or the original Portuguese text
when a language is missing a entry. Interpolation uses ``str.format`` named
fields: ``_("Caixa de Entrada  {count}", count=3)``.
"""

from __future__ import annotations

from organizador.config import DEFAULT_LANGUAGE, LANGUAGE_IDS
from organizador.i18n_data import EN_STRINGS, ES_STRINGS, FR_STRINGS

LANGUAGE_NAMES: dict[str, str] = {
    "pt": "Português (Portugal)",
    "en": "English",
    "es": "Español",
    "fr": "Français",
}

_translations: dict[str, dict[str, str]] = {
    "en": EN_STRINGS,
    "es": ES_STRINGS,
    "fr": FR_STRINGS,
}
_active: str = DEFAULT_LANGUAGE


def set_language(code: str) -> None:
    """Select the active language, ignoring unknown codes."""

    global _active
    _active = code if code in LANGUAGE_IDS else DEFAULT_LANGUAGE


def current_language() -> str:
    """Return the active language code."""

    return _active


def register(language: str, strings: dict[str, str]) -> None:
    """Install one language's translations (replacing any previous set)."""

    if language in LANGUAGE_NAMES:
        _translations[language] = strings


def known_source_strings() -> set[str]:
    """Return every source string that has at least one translation."""

    keys: set[str] = set()
    for strings in _translations.values():
        keys.update(strings)
    return keys


def _(source: str, **kwargs: object) -> str:
    """Translate a Portuguese source string into the active language."""

    template = _translations.get(_active, {}).get(source, source)
    return template.format(**kwargs) if kwargs else template
