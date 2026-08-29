"""Domain models shared by the application layers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

FILE_KINDS: tuple[str, ...] = (
    "Slides",
    "Exercícios",
    "Testes",
    "Trabalhos",
    "Outros",
)


@dataclass(frozen=True, slots=True)
class Subject:
    """A university subject and its filing rules."""

    id: int
    name: str
    code: str
    color: str
    keywords: tuple[str, ...]
    folder_name: str
    active: bool = True


@dataclass(frozen=True, slots=True)
class InboxItem:
    """A downloaded file waiting for a filing decision."""

    id: int
    path: Path
    original_path: Path
    original_name: str
    size: int
    detected_at: datetime
    status: str
    suggested_subject_id: int | None
    suggested_kind: str
    last_error: str


@dataclass(frozen=True, slots=True)
class FiledDocument:
    """A document filed into a subject folder."""

    id: int
    subject_id: int
    kind: str
    original_name: str
    current_path: Path
    original_path: Path
    size: int
    filed_at: datetime
    indexed_at: datetime | None


@dataclass(frozen=True, slots=True)
class HistoryEvent:
    """A reversible file-system event."""

    id: int
    action: str
    source_path: Path
    destination_path: Path
    file_id: int | None
    inbox_id: int | None
    created_at: datetime
    undone_at: datetime | None


@dataclass(frozen=True, slots=True)
class StudyTask:
    """A subject-linked task or deadline."""

    id: int
    title: str
    subject_id: int | None
    subject_name: str
    file_id: int | None
    due_date: date | None
    completed: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SearchResult:
    """A page, slide or worksheet-level local search result."""

    file_id: int
    path: Path
    title: str
    subject_name: str
    kind: str
    page: int
    snippet: str


@dataclass(frozen=True, slots=True)
class FilingGuess:
    """The classifier's best subject and file-kind proposal."""

    subject_id: int | None
    kind: str
    confidence: int
