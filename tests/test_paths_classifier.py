"""Filename safety, classifier and deadline tests."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from organizador.classifier import extract_due_date, guess_filing, normalise
from organizador.models import FilingHint, Subject
from organizador.paths import sanitise_component, sanitise_filename, unique_path


def test_windows_components_remove_invalid_and_reserved_names() -> None:
    assert sanitise_component("  aula: 3 / revisão?  ") == "aula- 3 - revisão-"
    assert sanitise_component("CON") == "CON_"
    assert sanitise_filename(r"..\segredo?.pdf") == "segredo-.pdf"


def test_unique_path_adds_a_human_readable_counter(tmp_path: Path) -> None:
    (tmp_path / "Aula.pdf").write_bytes(b"one")
    (tmp_path / "Aula (2).pdf").write_bytes(b"two")

    assert unique_path(tmp_path, "Aula.pdf").name == "Aula (3).pdf"


def test_normalise_folds_portuguese_accents() -> None:
    assert normalise("CÁLCULO — Integração") == "calculo integracao"


def test_classifier_uses_code_keyword_and_document_type() -> None:
    subjects = [
        Subject(1, "Cálculo I", "MAT101", "#000000", ("derivadas",), "MAT101"),
        Subject(2, "Bases de Dados", "BD201", "#000000", ("sql",), "BD201"),
    ]

    guess = guess_filing("MAT101_ficha_derivadas_04.pdf", subjects)

    assert guess.subject_id == 1
    assert guess.kind == "Exercícios"
    assert guess.confidence >= 90


def test_classifier_does_not_force_an_unrelated_subject() -> None:
    subjects = [Subject(1, "Álgebra", "ALG", "#000000", (), "ALG")]

    guess = guess_filing("receita_de_bolo.pdf", subjects)

    assert guess.subject_id == 1  # A single configured subject remains a low-confidence shortcut.
    assert guess.confidence == 30


def test_classifier_learns_only_after_two_matching_confirmations() -> None:
    subjects = [
        Subject(1, "Física", "FIS", "#000000", (), "FIS"),
        Subject(2, "História", "HIS", "#000000", (), "HIS"),
    ]
    hints = [
        FilingHint("folha_semanal_01.pdf", 2, "Testes"),
        FilingHint("Folha Semanal 02.docx", 2, "Testes"),
    ]
    baseline = guess_filing("folha-semanal-03.pdf", subjects)

    assert guess_filing("folha-semanal-03.pdf", subjects, hints[:1]) == baseline

    learned = guess_filing("folha-semanal-03.pdf", subjects, hints)
    assert learned.subject_id == 2
    assert learned.kind == "Testes"
    assert learned.confidence == 70


def test_classifier_abstains_on_conflicting_learned_values() -> None:
    subjects = [
        Subject(1, "Física", "FIS", "#000000", (), "FIS"),
        Subject(2, "História", "HIS", "#000000", (), "HIS"),
    ]
    hints = [
        FilingHint("ficha_semanal_01.pdf", 1, "Slides"),
        FilingHint("ficha semanal 02.pdf", 1, "Trabalhos"),
    ]

    guess = guess_filing("ficha-semanal-03.pdf", subjects, hints)

    assert guess.subject_id == 1
    assert guess.kind == "Exercícios"
    assert guess.confidence == 70


def test_classifier_ignores_generic_or_invalid_learning() -> None:
    subjects = [Subject(1, "Física", "FIS", "#000000", (), "FIS")]
    invalid = [
        FilingHint("aula_01.pdf", 99, "Desconhecido"),
        FilingHint("aula_02.pdf", 99, "Desconhecido"),
    ]

    assert guess_filing("aula_03.pdf", subjects, invalid) == guess_filing("aula_03.pdf", subjects)


def test_due_date_supports_iso_and_portuguese_numeric_forms() -> None:
    assert extract_due_date("trabalho_2026-09-15.pdf") == date(2026, 9, 15)
    assert extract_due_date("entrega_30-09.pdf", today=date(2026, 8, 1)) == date(2026, 9, 30)
    assert extract_due_date("entrega_02-01.pdf", today=date(2026, 8, 1)) == date(2027, 1, 2)


def test_due_date_rejects_invalid_calendar_values() -> None:
    assert extract_due_date("entrega_2026-13-45.pdf") is None
    assert extract_due_date("sem_data.pdf") is None
