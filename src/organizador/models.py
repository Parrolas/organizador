"""Domain models shared by the application layers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from stat import S_ISREG

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
class FilingHint:
    """A confirmed filing choice available to the local classifier."""

    original_name: str
    subject_id: int
    kind: str


@dataclass(frozen=True, slots=True)
class ExistingDownload:
    """A top-level Downloads file captured before manual import confirmation."""

    path: Path
    size: int
    modified_ns: int
    device: int
    inode: int

    @classmethod
    def capture(cls, path: Path) -> ExistingDownload | None:
        """Capture a regular non-link file without following filesystem links."""

        candidate = path.absolute()
        try:
            details = candidate.lstat()
        except OSError:
            return None
        if not S_ISREG(details.st_mode):
            return None
        return cls(
            candidate,
            details.st_size,
            details.st_mtime_ns,
            details.st_dev,
            details.st_ino,
        )

    def still_matches(self) -> bool:
        """Return whether the same unchanged regular file is still present."""

        return ExistingDownload.capture(self.path) == self


@dataclass(frozen=True, slots=True)
class ExistingDownloadsPlan:
    """A read-only, capped plan awaiting explicit user confirmation."""

    total: int
    selected: tuple[ExistingDownload, ...]


@dataclass(frozen=True, slots=True)
class FilingGuess:
    """The classifier's best subject and file-kind proposal."""

    subject_id: int | None
    kind: str
    confidence: int
