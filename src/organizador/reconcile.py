"""Read-only startup scans with database-only recovery actions."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from stat import S_ISREG

from organizador.config import AppConfig
from organizador.db import Database
from organizador.models import (
    FILE_KINDS,
    ExistingDownload,
    InboxItem,
    InterruptedUndo,
    ReconciliationOutcome,
    ReconciliationReport,
)

LOGGER = logging.getLogger(__name__)
SCAN_LIMIT = 500
RECOVERY_ERROR = (
    "Recuperação necessária: o ficheiro já não está na Caixa de Entrada. "
    "Procura-o nas pastas Universidade e Downloads antes de marcar uma nova organização."
)


class _ProbeState(Enum):
    MISSING = auto()
    UNSAFE = auto()
    UNREADABLE = auto()


PathProbe = ExistingDownload | _ProbeState


@dataclass(slots=True)
class _ScanState:
    remaining: int = field(default_factory=lambda: SCAN_LIMIT)
    truncated: bool = False
    incomplete: bool = False


def scan(config: AppConfig, database: Database) -> ReconciliationReport:
    """Inspect known folders and persistence without changing either one."""

    state = _ScanState()
    inbox_items = database.list_inbox_for_reconciliation()
    documents = database.list_files()
    pending_filings = tuple(database.list_pending_filings())
    pending_returns = tuple(database.list_pending_returns())
    pending_undos = tuple(database.list_pending_undos())
    pending_inbox_ids = {
        event.inbox_id
        for event in (*pending_filings, *pending_returns)
        if event.inbox_id is not None
    }
    known_inbox_paths = {
        item.path.absolute()
        for item in inbox_items
        if item.status in {"pending", "error", "filing", "returning"}
    }
    known_inbox_paths.update(event.destination_path.absolute() for event in pending_undos)
    tracked_document_paths = {document.current_path.absolute() for document in documents}
    tracked_document_paths.update(event.destination_path.absolute() for event in pending_filings)
    inbox_orphans: list[ExistingDownload] = []
    untracked_subject_files: list[Path] = []

    for candidate in _regular_files(config.inbox_dir, state):
        if candidate.path in known_inbox_paths or candidate.path in tracked_document_paths:
            continue
        if config.accepts(candidate.path) and candidate.size >= config.minimum_file_size:
            inbox_orphans.append(candidate)

    for subject in database.list_subjects(active_only=False):
        for kind in FILE_KINDS:
            folder = config.university_root / subject.folder_name / kind
            for candidate in _regular_files(folder, state):
                if candidate.path not in tracked_document_paths:
                    untracked_subject_files.append(candidate.path)

    inbox_probes = {
        item.id: _probe(item.path, state)
        for item in inbox_items
        if item.status in {"pending", "error", "filing", "returning"}
    }
    interrupted = tuple(
        item
        for item in inbox_items
        if item.status == "filing"
        and item.id not in pending_inbox_ids
        and isinstance(inbox_probes[item.id], ExistingDownload)
    )
    missing_inbox = tuple(
        item
        for item in inbox_items
        if item.status in {"pending", "error", "filing", "returning"}
        and item.id not in pending_inbox_ids
        and inbox_probes[item.id] is _ProbeState.MISSING
    )

    document_by_id = {document.id: document for document in documents}
    document_probes = {document.id: _probe(document.current_path, state) for document in documents}
    pending_file_ids = {event.file_id for event in pending_undos if event.file_id is not None}
    undoable_filings = database.list_undoable_filings()
    undo_probes = {event.id: _probe(event.destination_path, state) for event in undoable_filings}
    broken_candidates = tuple(
        event for event in undoable_filings if undo_probes[event.id] is _ProbeState.MISSING
    )
    orphan_by_path = {candidate.path: candidate for candidate in inbox_orphans}
    legacy_undos: list[InterruptedUndo] = []
    legacy_event_ids: set[int] = set()
    legacy_file_ids: set[int] = set()
    matched_orphan_paths: set[Path] = set()
    for event in broken_candidates:
        if event.file_id is None or event.file_id in pending_file_ids:
            continue
        restored_candidate = orphan_by_path.get(event.source_path.absolute())
        document = document_by_id.get(event.file_id)
        if (
            restored_candidate is None
            or document is None
            or restored_candidate.size != document.size
        ):
            continue
        legacy_undos.append(InterruptedUndo(event, restored_candidate))
        legacy_event_ids.add(event.id)
        legacy_file_ids.add(event.file_id)
        matched_orphan_paths.add(restored_candidate.path)

    repairable_file_ids = pending_file_ids | legacy_file_ids
    missing_documents = tuple(
        document
        for document in documents
        if document.id not in repairable_file_ids
        and document_probes[document.id] is _ProbeState.MISSING
    )
    broken_undo = tuple(
        event
        for event in broken_candidates
        if event.file_id not in pending_file_ids and event.id not in legacy_event_ids
    )
    unsafe_paths = {
        *(item.path for item in inbox_items if inbox_probes.get(item.id) is _ProbeState.UNSAFE),
        *(
            document.current_path
            for document in documents
            if document_probes[document.id] is _ProbeState.UNSAFE
        ),
        *(
            event.destination_path
            for event in undoable_filings
            if undo_probes[event.id] is _ProbeState.UNSAFE
        ),
    }
    for event in (*pending_filings, *pending_returns, *pending_undos):
        for path in (event.source_path, event.destination_path):
            if _probe(path, state) is _ProbeState.UNSAFE:
                unsafe_paths.add(path)
    return ReconciliationReport(
        inbox_orphans=tuple(
            candidate for candidate in inbox_orphans if candidate.path not in matched_orphan_paths
        ),
        interrupted_filings=interrupted,
        missing_inbox_items=missing_inbox,
        untracked_subject_files=tuple(untracked_subject_files),
        missing_documents=missing_documents,
        broken_undo_events=broken_undo,
        pending_filing_events=pending_filings,
        pending_return_events=pending_returns,
        pending_undo_events=pending_undos,
        legacy_interrupted_undos=tuple(legacy_undos),
        unsafe_paths=tuple(sorted(unsafe_paths, key=lambda path: str(path).casefold())),
        truncated=state.truncated,
        incomplete=state.incomplete,
    )


def apply(database: Database, report: ReconciliationReport) -> ReconciliationOutcome:
    """Apply additive or status-only repairs and never mutate user files."""

    recovered_items: list[InboxItem] = []
    reset_filing_ids: list[int] = []
    recovery_required_ids: list[int] = []
    completed_undo_event_ids: list[int] = []
    cancelled_undo_event_ids: list[int] = []
    completed_operation_event_ids: list[int] = []
    cancelled_operation_event_ids: list[int] = []

    for pending in report.pending_filing_events:
        source = _probe(pending.source_path)
        destination = _probe(pending.destination_path)
        if (
            source is _ProbeState.MISSING
            and isinstance(destination, ExistingDownload)
            and pending.inbox_id is not None
            and pending.subject_id is not None
            and pending.kind in FILE_KINDS
        ):
            try:
                database.record_filing(
                    pending.inbox_id,
                    pending.subject_id,
                    pending.kind,
                    pending.destination_path,
                    pending_event_id=pending.id,
                )
            except LookupError:
                pass
            else:
                completed_operation_event_ids.append(pending.id)
        elif (
            isinstance(source, ExistingDownload)
            and destination is _ProbeState.MISSING
            and database.cancel_pending_inbox_operation(
                pending.id, current_path=pending.source_path
            )
        ):
            cancelled_operation_event_ids.append(pending.id)

    for pending in report.pending_return_events:
        source = _probe(pending.source_path)
        destination = _probe(pending.destination_path)
        if (
            source is _ProbeState.MISSING
            and isinstance(destination, ExistingDownload)
            and pending.inbox_id is not None
        ):
            try:
                database.record_return(
                    pending.inbox_id,
                    pending.destination_path,
                    pending_event_id=pending.id,
                )
            except LookupError:
                pass
            else:
                completed_operation_event_ids.append(pending.id)
        elif (
            isinstance(source, ExistingDownload)
            and destination is _ProbeState.MISSING
            and database.cancel_pending_inbox_operation(
                pending.id, current_path=pending.source_path
            )
        ):
            cancelled_operation_event_ids.append(pending.id)

    for pending in report.pending_undo_events:
        source = _probe(pending.source_path)
        restored = _probe(pending.destination_path)
        if source is _ProbeState.MISSING and isinstance(restored, ExistingDownload):
            item = database.complete_pending_undo(pending)
            if item is not None:
                recovered_items.append(item)
                completed_undo_event_ids.append(pending.id)
        elif isinstance(source, ExistingDownload) and restored is _ProbeState.MISSING:
            if database.cancel_pending_undo(pending.id):
                cancelled_undo_event_ids.append(pending.id)

    for interrupted_undo in report.legacy_interrupted_undos:
        candidate = interrupted_undo.restored_file
        event = interrupted_undo.event
        if not candidate.still_matches():
            continue
        if _probe(event.destination_path) is not _ProbeState.MISSING:
            continue
        document = database.get_file(event.file_id) if event.file_id is not None else None
        if document is None or document.size != candidate.size:
            continue
        item = database.complete_legacy_interrupted_undo(event, candidate.path)
        if item is not None:
            recovered_items.append(item)
            completed_undo_event_ids.append(event.id)

    for candidate in report.inbox_orphans:
        if not candidate.still_matches():
            continue
        if database.find_active_inbox_by_path(candidate.path) is not None:
            continue
        try:
            item = database.register_recovered_inbox_file(candidate.path, candidate.size)
        except sqlite3.IntegrityError:
            LOGGER.info("Inbox orphan was recovered concurrently: %s", candidate.path)
            continue
        if item is not None:
            recovered_items.append(item)

    for item in report.interrupted_filings:
        if not isinstance(_probe(item.path), ExistingDownload):
            continue
        if database.reset_interrupted_filing(item.id):
            reset_filing_ids.append(item.id)

    for item in report.missing_inbox_items:
        if _probe(item.path) is not _ProbeState.MISSING:
            continue
        if database.mark_inbox_recovery_required(item.id, RECOVERY_ERROR):
            recovery_required_ids.append(item.id)

    return ReconciliationOutcome(
        recovered_items=tuple(recovered_items),
        reset_filing_ids=tuple(reset_filing_ids),
        recovery_required_ids=tuple(recovery_required_ids),
        completed_undo_event_ids=tuple(completed_undo_event_ids),
        cancelled_undo_event_ids=tuple(cancelled_undo_event_ids),
        completed_operation_event_ids=tuple(completed_operation_event_ids),
        cancelled_operation_event_ids=tuple(cancelled_operation_event_ids),
    )


def _probe(path: Path, state: _ScanState | None = None) -> PathProbe:
    candidate = path.absolute()
    try:
        details = candidate.lstat()
    except FileNotFoundError:
        return _ProbeState.MISSING
    except OSError:
        if state is not None:
            state.incomplete = True
        LOGGER.exception("Could not inspect recovery path %s", candidate)
        return _ProbeState.UNREADABLE
    if not S_ISREG(details.st_mode):
        return _ProbeState.UNSAFE
    return ExistingDownload(
        candidate,
        details.st_size,
        details.st_mtime_ns,
        details.st_dev,
        details.st_ino,
    )


def _regular_files(directory: Path, state: _ScanState) -> Iterator[ExistingDownload]:
    try:
        for path in directory.iterdir():
            if state.remaining <= 0:
                state.truncated = True
                return
            state.remaining -= 1
            candidate = _probe(path, state)
            if isinstance(candidate, ExistingDownload):
                yield candidate
    except FileNotFoundError:
        return
    except OSError:
        state.incomplete = True
        LOGGER.exception("Could not scan recovery directory %s", directory)
