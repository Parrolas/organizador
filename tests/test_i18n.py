"""Translation coverage and behaviour tests."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from organizador import i18n
from organizador.config import LANGUAGE_IDS
from organizador.i18n_data import EN_STRINGS, ES_STRINGS, FR_STRINGS


def _source_translation_literals() -> set[str]:
    """Collect every literal passed as the first argument to ``_()`` in src."""

    root = Path(__file__).resolve().parent.parent / "src" / "organizador"
    literals: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                literals.add(node.args[0].value)
    return literals


def test_every_source_literal_has_a_translation() -> None:
    missing = sorted(
        literal for literal in _source_translation_literals() if literal not in EN_STRINGS
    )

    assert missing == []


def _format_fields(template: str) -> set[str]:
    import string

    return {field for _, field, _, _ in string.Formatter().parse(template) if field}


def test_all_languages_share_the_same_key_set() -> None:
    expected = set(EN_STRINGS)
    assert set(ES_STRINGS) == expected
    assert set(FR_STRINGS) == expected


def test_format_placeholders_match_across_languages() -> None:
    for key, en in EN_STRINGS.items():
        assert _format_fields(ES_STRINGS[key]) == _format_fields(en), key
        assert _format_fields(FR_STRINGS[key]) == _format_fields(en), key


def test_translations_differ_from_the_portuguese_source() -> None:
    # A translation that equals the key for a long sentence is likely a stub.
    # Pure placeholder templates (only separators between placeholders) are exempt.
    pattern = re.compile(r"\{[^}]*\}")
    samples = [
        key
        for key in EN_STRINGS
        if len(key) > 25 and any(char.isalpha() for char in pattern.sub("", key))
    ]
    assert samples
    for key in samples:
        assert EN_STRINGS[key] != key


@pytest.mark.parametrize("code", list(LANGUAGE_IDS))
def test_set_language_switches_translation(code: str) -> None:
    i18n.set_language(code)
    try:
        translated = i18n._("Caixa de Entrada")
        if code == "pt":
            assert translated == "Caixa de Entrada"
        else:
            assert translated != "Caixa de Entrada"
    finally:
        i18n.set_language("pt")


def test_unknown_language_falls_back_to_portuguese() -> None:
    i18n.set_language("xx")
    try:
        assert i18n._("Caixa de Entrada") == "Caixa de Entrada"
    finally:
        i18n.set_language("pt")


def test_interpolation_uses_named_fields() -> None:
    i18n.set_language("en")
    try:
        assert i18n._("{count} ficheiro organizado", count=3) == "3 file organized"
    finally:
        i18n.set_language("pt")
