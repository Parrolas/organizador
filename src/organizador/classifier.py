"""Transparent filename-based subject, type and deadline suggestions."""

from __future__ import annotations

import re
import unicodedata
from datetime import date

from rapidfuzz import fuzz

from organizador.models import FilingGuess, Subject

KIND_TERMS: dict[str, tuple[str, ...]] = {
    "Slides": ("slide", "slides", "aula", "lecture", "capitulo", "chapter", "teoria"),
    "Exercícios": (
        "exercicio",
        "exercicios",
        "ficha",
        "problema",
        "problemset",
        "exercise",
        "lista",
    ),
    "Testes": ("teste", "test", "exame", "exam", "quiz", "frequencia", "avaliacao"),
    "Trabalhos": (
        "trabalho",
        "assignment",
        "projeto",
        "project",
        "relatorio",
        "report",
        "entrega",
        "homework",
    ),
}


def normalise(value: str) -> str:
    """Fold accents, case and punctuation for predictable matching."""

    decomposed = unicodedata.normalize("NFKD", value)
    ascii_like = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    return " ".join(re.findall(r"[a-z0-9]+", ascii_like.casefold()))


def guess_filing(filename: str, subjects: list[Subject]) -> FilingGuess:
    """Suggest a subject and document kind from a filename."""

    normalised_name = normalise(filename.rsplit(".", 1)[0])
    tokens = set(normalised_name.split())
    kind = _guess_kind(tokens, normalised_name)
    best_subject_id: int | None = None
    best_score = 0

    for subject in subjects:
        subject_name = normalise(subject.name)
        code = normalise(subject.code)
        score = int(fuzz.WRatio(normalised_name, subject_name) * 0.42)
        if subject_name and subject_name in normalised_name:
            score += 55
        if code and (code in tokens or code.replace(" ", "") in normalised_name.replace(" ", "")):
            score += 90
        for keyword in subject.keywords:
            normalised_keyword = normalise(keyword)
            if not normalised_keyword:
                continue
            if normalised_keyword in tokens:
                score += 72
            elif normalised_keyword in normalised_name:
                score += 44
        if score > best_score:
            best_score = score
            best_subject_id = subject.id

    if len(subjects) == 1 and best_score < 30:
        best_subject_id = subjects[0].id
        best_score = 30
    if best_score < 28:
        best_subject_id = None
    return FilingGuess(best_subject_id, kind, min(100, best_score))


def _guess_kind(tokens: set[str], normalised_name: str) -> str:
    scores: dict[str, int] = {}
    for kind, terms in KIND_TERMS.items():
        score = 0
        for term in terms:
            if term in tokens:
                score += 3
            elif term in normalised_name:
                score += 1
        scores[kind] = score
    best_kind = max(scores, key=scores.__getitem__)
    return best_kind if scores[best_kind] else "Outros"


def extract_due_date(filename: str, *, today: date | None = None) -> date | None:
    """Extract common numeric deadline formats from a filename."""

    reference = today or date.today()
    stem = filename.rsplit(".", 1)[0]
    iso_match = re.search(r"(?<!\d)(20\d{2})[-_.](\d{1,2})[-_.](\d{1,2})(?!\d)", stem)
    if iso_match:
        try:
            return date(*(int(value) for value in iso_match.groups()))
        except ValueError:
            return None

    local_match = re.search(r"(?<!\d)(\d{1,2})[-_.](\d{1,2})(?:[-_.](20\d{2}))?(?!\d)", stem)
    if not local_match:
        return None
    day, month, year = local_match.groups()
    try:
        candidate = date(int(year) if year else reference.year, int(month), int(day))
    except ValueError:
        return None
    if year is None and candidate < reference:
        try:
            candidate = candidate.replace(year=reference.year + 1)
        except ValueError:
            return None
    return candidate
