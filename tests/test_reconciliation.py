"""Startup consistency checks that never mutate user documents."""

from __future__ import annotations

from pathlib import Path

import pytest

from organizador import reconcile
from organizador.config import AppConfig
from organizador.db import Database
from organizador.filer import FilingService
from organizador.models import Subject
from organizador.reconcile import apply, scan


def _snapshot(root: Path) -> dict[Path, bytes]:
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_scan_and_apply_recover_only_safe_database_states(
    app_config: AppConfig,
    database: Database,
    subject: Subject,
) -> None:
    orphan = app_config.inbox_dir / "MAT101_recuperado.pdf"
    orphan.write_bytes(b"untracked inbox document")

    interrupted_path = app_config.inbox_dir / "interrompido.pdf"
    interrupted_path.write_bytes(b"interrupted filing document")
    interrupted = database.add_inbox_item(
        interrupted_path,
        app_config.downloads_dir / interrupted_path.name,
        interrupted_path.name,
        interrupted_path.stat().st_size,
    )
    database.set_inbox_status(interrupted.id, "filing")

    missing_path = app_config.inbox_dir / "desaparecido.pdf"
    missing = database.add_inbox_item(
        missing_path,
        app_config.downloads_dir / missing_path.name,
        missing_path.name,
        200,
    )

    subject_folder = app_config.university_root / subject.folder_name / "Slides"
    untracked_subject = subject_folder / "sem-registo.pdf"
    untracked_subject.write_bytes(b"untracked subject document")

    filed_source = app_config.inbox_dir / "organizado.pdf"
    filed_source.write_bytes(b"filed document")
    filed_item = database.add_inbox_item(
        filed_source,
        app_config.downloads_dir / filed_source.name,
        filed_source.name,
        filed_source.stat().st_size,
    )
    filed_destination = subject_folder / "organizado.pdf"
    filed_source.replace(filed_destination)
    missing_document = database.record_filing(
        filed_item.id,
        subject.id,
        "Slides",
        filed_destination,
    )
    filed_destination.unlink()

    downloads_file = app_config.downloads_dir / "nunca-analisar.pdf"
    downloads_file.write_bytes(b"Downloads remains outside reconciliation")
    before = _snapshot(app_config.university_root)

    report = scan(app_config, database)

    assert _snapshot(app_config.university_root) == before
    assert [candidate.path for candidate in report.inbox_orphans] == [orphan]
    assert [item.id for item in report.interrupted_filings] == [interrupted.id]
    assert report.missing_inbox_items == (missing,)
    assert report.untracked_subject_files == (untracked_subject,)
    assert report.missing_documents == (missing_document,)
    assert len(report.broken_undo_events) == 1
    assert report.broken_undo_events[0].destination_path == filed_destination
    assert downloads_file not in {
        *(candidate.path for candidate in report.inbox_orphans),
        *report.untracked_subject_files,
    }

    outcome = apply(database, report)

    assert _snapshot(app_config.university_root) == before
    assert [item.path for item in outcome.recovered_items] == [orphan]
    assert outcome.reset_filing_ids == (interrupted.id,)
    assert outcome.recovery_required_ids == (missing.id,)
    recovered_interrupted = database.get_inbox_item(interrupted.id)
    assert recovered_interrupted is not None
    assert recovered_interrupted.status == "pending"
    recovered_missing = database.get_inbox_item(missing.id)
    assert recovered_missing is not None
    assert recovered_missing.status == "recovery"
    assert "Recuperação necessária" in recovered_missing.last_error
    assert database.get_file(missing_document.id) == missing_document
    assert untracked_subject.read_bytes() == b"untracked subject document"
    assert downloads_file.read_bytes() == b"Downloads remains outside reconciliation"

    second_outcome = apply(database, scan(app_config, database))

    assert second_outcome.change_count == 0
    assert _snapshot(app_config.university_root) == before


def test_recovery_record_is_reactivated_if_the_file_returns_to_inbox(
    app_config: AppConfig,
    database: Database,
) -> None:
    path = app_config.inbox_dir / "restaurado.pdf"
    item = database.add_inbox_item(
        path,
        app_config.downloads_dir / path.name,
        path.name,
        100,
    )
    first_outcome = apply(database, scan(app_config, database))
    assert first_outcome.recovery_required_ids == (item.id,)

    path.write_bytes(b"manually restored file")
    report = scan(app_config, database)
    assert [candidate.path for candidate in report.inbox_orphans] == [path]

    second_outcome = apply(database, report)

    assert [recovered.id for recovered in second_outcome.recovered_items] == [item.id]
    restored = database.get_inbox_item(item.id)
    assert restored is not None
    assert restored.status == "pending"
    assert restored.size == path.stat().st_size
    assert restored.last_error == ""


def test_changed_orphan_is_not_registered_from_a_stale_scan(
    app_config: AppConfig,
    database: Database,
) -> None:
    path = app_config.inbox_dir / "mudou.pdf"
    path.write_bytes(b"original orphan file")
    report = scan(app_config, database)
    path.write_bytes(b"changed after the read-only scan")

    outcome = apply(database, report)

    assert outcome.change_count == 0
    assert database.find_active_inbox_by_path(path) is None
    assert path.read_bytes() == b"changed after the read-only scan"


def test_unreadable_file_is_never_persisted_as_missing(
    app_config: AppConfig,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = app_config.inbox_dir / "temporariamente-inacessivel.pdf"
    path.write_bytes(b"still present")
    item = database.add_inbox_item(
        path,
        app_config.downloads_dir / path.name,
        path.name,
        path.stat().st_size,
    )
    original_lstat = Path.lstat

    def deny_target(candidate: Path) -> object:
        if candidate == path:
            raise PermissionError("temporarily unavailable")
        return original_lstat(candidate)

    monkeypatch.setattr(Path, "lstat", deny_target)

    report = scan(app_config, database)
    outcome = apply(database, report)

    assert report.incomplete
    assert report.missing_inbox_items == ()
    assert outcome.change_count == 0
    unchanged = database.get_inbox_item(item.id)
    assert unchanged is not None
    assert unchanged.status == "pending"


def test_unreadable_directory_marks_scan_incomplete_without_registering_files(
    app_config: AppConfig,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orphan = app_config.inbox_dir / "nao-observado.pdf"
    orphan.write_bytes(b"must remain unregistered")
    original_iterdir = Path.iterdir

    def deny_inbox(candidate: Path) -> object:
        if candidate == app_config.inbox_dir:
            raise PermissionError("directory temporarily unavailable")
        return original_iterdir(candidate)

    monkeypatch.setattr(Path, "iterdir", deny_inbox)

    report = scan(app_config, database)
    outcome = apply(database, report)

    assert report.incomplete
    assert report.inbox_orphans == ()
    assert outcome.change_count == 0
    assert database.find_active_inbox_by_path(orphan) is None
    assert orphan.read_bytes() == b"must remain unregistered"


def test_prepared_undo_is_completed_after_a_crash_between_move_and_commit(
    app_config: AppConfig,
    database: Database,
    filer: FilingService,
    subject: Subject,
) -> None:
    source = app_config.downloads_dir / "desfazer-interrompido.pdf"
    source.write_bytes(b"undo crash boundary" * 20)
    item = filer.ingest(source)
    assert item is not None
    document = filer.file_document(item.id, subject.id, "Slides", source.name)
    event = database.latest_undoable_filing()
    assert event is not None
    document.current_path.write_bytes(b"edited after filing" * 40)
    restored_path = event.source_path
    pending = database.begin_filing_undo(event, restored_path)
    document.current_path.replace(restored_path)
    before = _snapshot(app_config.university_root)

    report = scan(app_config, database)

    assert report.pending_undo_events == (pending,)
    assert report.inbox_orphans == ()
    assert report.missing_documents == ()
    assert report.broken_undo_events == ()

    outcome = apply(database, report)

    assert _snapshot(app_config.university_root) == before
    assert outcome.completed_undo_event_ids == (pending.id,)
    assert [recovered.id for recovered in outcome.recovered_items] == [item.id]
    restored = database.get_inbox_item(item.id)
    assert restored is not None
    assert restored.status == "pending"
    assert restored.path == restored_path
    assert database.get_file(document.id) is None
    assert database.list_pending_undos() == []
    assert database.latest_undoable_filing() is None


def test_prepared_undo_is_cancelled_when_the_move_never_started(
    app_config: AppConfig,
    database: Database,
    filer: FilingService,
    subject: Subject,
) -> None:
    source = app_config.downloads_dir / "desfazer-nao-iniciado.pdf"
    source.write_bytes(b"undo did not start" * 20)
    item = filer.ingest(source)
    assert item is not None
    document = filer.file_document(item.id, subject.id, "Slides", source.name)
    event = database.latest_undoable_filing()
    assert event is not None
    pending = database.begin_filing_undo(event, event.source_path)

    outcome = apply(database, scan(app_config, database))

    assert outcome.cancelled_undo_event_ids == (pending.id,)
    assert database.list_pending_undos() == []
    assert document.current_path.exists()
    assert database.get_file(document.id) == document
    assert database.latest_undoable_filing() == event


def test_legacy_interrupted_undo_reuses_the_original_inbox_record(
    app_config: AppConfig,
    database: Database,
    filer: FilingService,
    subject: Subject,
) -> None:
    source = app_config.downloads_dir / "desfazer-antigo.pdf"
    source.write_bytes(b"legacy interrupted undo" * 20)
    item = filer.ingest(source)
    assert item is not None
    document = filer.file_document(item.id, subject.id, "Slides", source.name)
    event = database.latest_undoable_filing()
    assert event is not None
    document.current_path.replace(event.source_path)

    report = scan(app_config, database)

    assert [undo.event.id for undo in report.legacy_interrupted_undos] == [event.id]
    assert report.inbox_orphans == ()
    assert report.broken_undo_events == ()

    outcome = apply(database, report)

    assert outcome.completed_undo_event_ids == (event.id,)
    assert [recovered.id for recovered in outcome.recovered_items] == [item.id]
    assert database.get_file(document.id) is None
    assert database.count_inbox_items() == 1


def test_prepared_filing_is_completed_after_a_post_move_crash(
    app_config: AppConfig,
    database: Database,
    filer: FilingService,
    subject: Subject,
) -> None:
    source = app_config.downloads_dir / "arquivo-interrompido.pdf"
    source.write_bytes(b"pending filing" * 20)
    item = filer.ingest(source)
    assert item is not None
    destination = app_config.university_root / subject.folder_name / "Slides" / source.name
    pending = database.begin_document_filing(item.id, subject.id, "Slides", destination)
    item.path.replace(destination)
    before = _snapshot(app_config.university_root)

    report = scan(app_config, database)

    assert report.pending_filing_events == (pending,)
    assert report.untracked_subject_files == ()
    assert report.missing_inbox_items == ()

    outcome = apply(database, report)

    assert _snapshot(app_config.university_root) == before
    assert outcome.completed_operation_event_ids == (pending.id,)
    document = database.list_files()[0]
    assert document.current_path == destination
    filed_item = database.get_inbox_item(item.id)
    assert filed_item is not None
    assert filed_item.status == "filed"
    assert database.list_pending_filings() == []

    repeated = apply(database, report)
    assert repeated.change_count == 0
    assert len(database.list_files()) == 1


def test_prepared_return_is_completed_after_a_post_move_crash(
    app_config: AppConfig,
    database: Database,
    filer: FilingService,
) -> None:
    source = app_config.downloads_dir / "devolucao-interrompida.pdf"
    source.write_bytes(b"pending return" * 20)
    item = filer.ingest(source)
    assert item is not None
    destination = app_config.downloads_dir / source.name
    pending = database.begin_return(item.id, destination)
    item.path.replace(destination)
    before = destination.read_bytes()

    report = scan(app_config, database)
    outcome = apply(database, report)

    assert report.pending_return_events == (pending,)
    assert outcome.completed_operation_event_ids == (pending.id,)
    assert destination.read_bytes() == before
    returned = database.get_inbox_item(item.id)
    assert returned is not None
    assert returned.status == "returned"
    assert database.list_pending_returns() == []

    repeated = apply(database, report)
    assert repeated.change_count == 0
    with database.connect() as connection:
        return_events = int(
            connection.execute("SELECT COUNT(*) FROM events WHERE action = 'return'").fetchone()[0]
        )
    assert return_events == 1


def test_non_regular_tracked_path_requires_manual_review(
    app_config: AppConfig,
    database: Database,
) -> None:
    path = app_config.inbox_dir / "substituido.pdf"
    item = database.add_inbox_item(
        path,
        app_config.downloads_dir / path.name,
        path.name,
        100,
    )
    path.mkdir()

    report = scan(app_config, database)
    outcome = apply(database, report)

    assert report.unsafe_paths == (path,)
    assert report.missing_inbox_items == ()
    assert outcome.change_count == 0
    unchanged = database.get_inbox_item(item.id)
    assert unchanged is not None
    assert unchanged.status == "pending"
    assert path.is_dir()


def test_scan_limit_bounds_directory_work(
    app_config: AppConfig,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = app_config.inbox_dir / "primeiro.pdf"
    second = app_config.inbox_dir / "segundo.pdf"
    first.write_bytes(b"first orphan file")
    second.write_bytes(b"second orphan file")
    monkeypatch.setattr(reconcile, "SCAN_LIMIT", 1)

    report = scan(app_config, database)

    assert report.truncated
    assert len(report.inbox_orphans) == 1
    assert first.exists()
    assert second.exists()
