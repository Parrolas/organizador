"""SQLite repository and FTS behavior tests."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from organizador.db import Database
from organizador.models import Subject


def _file_record(database: Database, subject: Subject, tmp_path: Path) -> int:
    inbox_path = tmp_path / "inbox" / "aula.txt"
    original_path = tmp_path / "downloads" / "aula.txt"
    inbox_path.parent.mkdir(parents=True)
    inbox_path.write_text("Derivadas e integrais", encoding="utf-8")
    item = database.add_inbox_item(inbox_path, original_path, "aula.txt", inbox_path.stat().st_size)
    destination = tmp_path / "subject" / "aula.txt"
    destination.parent.mkdir()
    inbox_path.replace(destination)
    return database.record_filing(item.id, subject.id, "Slides", destination).id


def test_subject_update_and_archive(database: Database, subject: Subject) -> None:
    updated = database.update_subject(
        subject.id,
        "Cálculo Diferencial",
        "MAT101",
        "#123456",
        ("limites", "derivadas"),
        subject.folder_name,
    )
    assert updated.name == "Cálculo Diferencial"
    assert updated.keywords == ("limites", "derivadas")

    database.archive_subject(subject.id)

    assert database.count_subjects() == 0
    assert database.get_subject(subject.id) is not None


def test_unused_subject_can_be_deleted_during_setup_rollback(
    database: Database, subject: Subject
) -> None:
    database.delete_subject(subject.id)

    assert database.get_subject(subject.id) is None


def test_fts_search_returns_page_and_prefix_match(
    database: Database, subject: Subject, tmp_path: Path
) -> None:
    file_id = _file_record(database, subject, tmp_path)
    database.replace_document_pages(
        file_id,
        subject.name,
        "aula.txt",
        ("A regra da cadeia calcula derivadas compostas.", "Integrais definidas."),
    )

    results = database.search("deriv")

    assert len(results) == 1
    assert results[0].page == 1
    assert "[derivadas]" in results[0].snippet.casefold()
    assert database.search("") == []


def test_tasks_order_completion_and_delete(database: Database, subject: Subject) -> None:
    later = database.add_task("Entrega final", subject.id, date(2026, 9, 20))
    sooner = database.add_task("Ficha 2", subject.id, date(2026, 9, 10))

    assert [task.id for task in database.list_tasks()] == [sooner.id, later.id]

    database.set_task_completed(sooner.id, True)
    tasks = database.list_tasks()
    assert tasks[-1].completed

    database.delete_task(later.id)
    assert database.get_task(later.id) is None


def test_mark_filing_undone_restores_inbox_state(
    database: Database, subject: Subject, tmp_path: Path
) -> None:
    file_id = _file_record(database, subject, tmp_path)
    event = database.latest_undoable_filing()
    assert event is not None
    restored = tmp_path / "inbox" / "restored.txt"

    database.mark_filing_undone(event, restored)

    assert database.get_file(file_id) is None
    item = database.get_inbox_item(event.inbox_id or -1)
    assert item is not None
    assert item.status == "pending"
    assert item.path == restored


def test_stale_index_job_cannot_write_to_a_reused_file_id(
    database: Database, subject: Subject, tmp_path: Path
) -> None:
    old_file_id = _file_record(database, subject, tmp_path / "old")
    old_document = database.get_file(old_file_id)
    assert old_document is not None
    old_path = old_document.current_path
    event = database.latest_undoable_filing()
    assert event is not None
    database.mark_filing_undone(event, tmp_path / "restored.txt")

    new_file_id = _file_record(database, subject, tmp_path / "new")
    assert new_file_id == old_file_id

    database.mark_document_indexed(old_file_id, expected_path=old_path)
    replacement = database.get_file(new_file_id)
    assert replacement is not None
    assert replacement.indexed_at is None

    database.replace_document_pages(
        old_file_id,
        subject.name,
        "old.txt",
        ("stale searchable text",),
        expected_path=old_path,
    )

    replacement = database.get_file(new_file_id)
    assert replacement is not None
    assert replacement.indexed_at is None
    assert database.search("stale") == []


def test_schema_migration_queues_existing_office_files_once(
    database: Database, subject: Subject, tmp_path: Path
) -> None:
    path = tmp_path / "slides.pptx"
    path.write_bytes(b"legacy presentation")
    item = database.add_inbox_item(
        path, tmp_path / "Downloads" / path.name, path.name, path.stat().st_size
    )
    document = database.record_filing(item.id, subject.id, "Slides", path)
    database.replace_document_pages(
        document.id,
        subject.name,
        document.original_name,
        ("legacy searchable slide",),
        expected_path=path,
    )
    assert database.search("legacy")
    with database.connect() as connection:
        connection.execute("PRAGMA user_version = 1")
        connection.commit()

    database.initialize()

    pending = database.get_file(document.id)
    assert pending is not None
    assert pending.indexed_at is None
    assert database.search("legacy") == []

    database.replace_document_pages(
        document.id,
        subject.name,
        document.original_name,
        ("reindexed presentation",),
        expected_path=path,
    )
    database.initialize()

    retained = database.get_file(document.id)
    assert retained is not None
    assert retained.indexed_at is not None
    assert database.search("reindexed")
