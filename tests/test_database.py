"""SQLite repository and FTS behavior tests."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from organizador.db import METRIC_COLLISIONS_RENAMED, Database
from organizador.models import (
    ActivitySummary,
    ExistingDownload,
    FilingHint,
    FindingReason,
    Subject,
)
from organizador.paths import normalise_path_key


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

    assert database.filing_hints() == [FilingHint("aula.txt", subject.id, "Slides")]

    database.mark_filing_undone(event, restored)

    assert database.get_file(file_id) is None
    item = database.get_inbox_item(event.inbox_id or -1)
    assert item is not None
    assert item.status == "pending"
    assert item.path == restored
    assert database.filing_hints() == []


def test_filing_hints_exclude_missing_files_and_archived_subjects(
    database: Database, subject: Subject, tmp_path: Path
) -> None:
    file_id = _file_record(database, subject, tmp_path)
    document = database.get_file(file_id)
    assert document is not None
    assert database.filing_hints() == [FilingHint("aula.txt", subject.id, "Slides")]

    document.current_path.unlink()
    assert database.filing_hints() == []

    document.current_path.write_text("restored", encoding="utf-8")
    database.archive_subject(subject.id)
    assert database.filing_hints() == []


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


def test_schema_v3_adds_operation_journal_metadata(database: Database) -> None:
    with database.connect() as connection:
        connection.execute("ALTER TABLE events DROP COLUMN subject_id")
        connection.execute("ALTER TABLE events DROP COLUMN kind")
        connection.execute("DROP INDEX idx_inbox_active_path")
        connection.execute(
            """
            CREATE UNIQUE INDEX idx_inbox_active_path
            ON inbox(path) WHERE status IN ('pending', 'error', 'filing')
            """
        )
        connection.execute("PRAGMA user_version = 2")
        connection.commit()

    database.initialize()

    with database.connect() as connection:
        columns = {
            str(row["name"]) for row in connection.execute("PRAGMA table_info(events)").fetchall()
        }
        index_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'idx_inbox_active_path'"
        ).fetchone()
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    assert {"subject_id", "kind"} <= columns
    assert index_row is not None
    assert "'returning'" in str(index_row["sql"])
    assert version == 5


def test_schema_v4_adds_file_origin_and_persisted_reviews(
    database: Database, subject: Subject, tmp_path: Path
) -> None:
    file_id = _file_record(database, subject, tmp_path)
    with database.connect() as connection:
        connection.execute("DROP TABLE reviewed_findings")
        connection.execute("ALTER TABLE files DROP COLUMN origin")
        connection.execute("ALTER TABLE files DROP COLUMN record_token")
        connection.execute("ALTER TABLE files DROP COLUMN catalog_state")
        connection.execute("PRAGMA user_version = 3")
        connection.commit()

    database.initialize()

    migrated = database.get_file(file_id)
    assert migrated is not None
    assert migrated.origin == "filed"
    assert migrated.record_token
    database.mark_finding_reviewed(
        migrated.current_path,
        FindingReason.MISSING_DOCUMENT.value,
    )
    database.initialize()
    with database.connect() as connection:
        columns = {
            str(row["name"]): row
            for row in connection.execute("PRAGMA table_info(files)").fetchall()
        }
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    assert columns["origin"]["dflt_value"] == "'filed'"
    assert columns["origin"]["notnull"] == 1
    assert columns["record_token"]["dflt_value"] == "''"
    assert columns["record_token"]["notnull"] == 1
    assert columns["catalog_state"]["dflt_value"] == "'active'"
    assert columns["catalog_state"]["notnull"] == 1
    assert version == 5
    assert database.list_reviewed_finding_keys() == {
        (
            normalise_path_key(migrated.current_path),
            FindingReason.MISSING_DOCUMENT.value,
        )
    }


def test_adopted_file_has_no_inbox_history_or_filing_hint(
    database: Database, subject: Subject, tmp_path: Path
) -> None:
    path = tmp_path / "adopted.txt"
    path.write_text("cataloged in place", encoding="utf-8")
    candidate = ExistingDownload.capture(path)
    assert candidate is not None

    document = database.adopt_subject_file(candidate, subject.id, "Outros")

    assert document.origin == "adopted"
    assert document.current_path == path.absolute()
    assert database.filing_hints() == []
    with database.connect() as connection:
        row = connection.execute(
            "SELECT inbox_id FROM files WHERE id = ?", (document.id,)
        ).fetchone()
        event_count = int(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0])
    assert row is not None
    assert row["inbox_id"] is None
    assert event_count == 0


def test_unregister_adopted_file_preserves_disk_and_clears_catalog_data(
    database: Database, subject: Subject, tmp_path: Path
) -> None:
    path = tmp_path / "adopted.txt"
    contents = b"searchable adopted document"
    path.write_bytes(contents)
    candidate = ExistingDownload.capture(path)
    assert candidate is not None
    document = database.adopt_subject_file(candidate, subject.id, "Outros")
    task = database.add_task("Review adopted", subject.id, None, document.id)
    database.replace_document_pages(
        document.id,
        subject.name,
        document.original_name,
        ("searchable adopted document",),
        expected_path=document.current_path,
        expected_record_token=document.record_token,
    )

    removed = database.unregister_adopted_file(
        document.id,
        expected_path=document.current_path,
        expected_record_token=document.record_token,
        reviewed_reason=FindingReason.UNTRACKED_SUBJECT_FILE.value,
    )

    assert removed
    assert path.read_bytes() == contents
    assert database.get_file(document.id) is None
    assert database.search("searchable") == []
    detached_task = database.get_task(task.id)
    assert detached_task is not None
    assert detached_task.file_id is None
    assert (
        normalise_path_key(path),
        FindingReason.UNTRACKED_SUBJECT_FILE.value,
    ) in database.list_reviewed_finding_keys()


def test_unregister_rejects_a_normally_filed_document(
    database: Database, subject: Subject, tmp_path: Path
) -> None:
    file_id = _file_record(database, subject, tmp_path)
    document = database.get_file(file_id)
    assert document is not None

    assert not database.unregister_adopted_file(
        document.id,
        expected_path=document.current_path,
        expected_record_token=document.record_token,
        reviewed_reason=FindingReason.UNTRACKED_SUBJECT_FILE.value,
    )
    assert database.get_file(document.id) == document


def test_reviewed_finding_keys_are_reason_specific_and_idempotent(
    database: Database, tmp_path: Path
) -> None:
    path = tmp_path / "finding.pdf"
    database.mark_finding_reviewed(path, FindingReason.MISSING_DOCUMENT.value)
    database.mark_finding_reviewed(path, FindingReason.MISSING_DOCUMENT.value)
    database.mark_finding_reviewed(path, FindingReason.BROKEN_UNDO_EVENT.value)

    keys = database.list_reviewed_finding_keys()

    assert len(keys) == 2
    assert {reason for _, reason in keys} == {
        FindingReason.MISSING_DOCUMENT.value,
        FindingReason.BROKEN_UNDO_EVENT.value,
    }


def test_stale_index_job_cannot_write_after_same_path_is_readopted(
    database: Database,
    subject: Subject,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("organizador.db._now", lambda: "2026-08-31T12:00:00+00:00")
    path = tmp_path / "same-path.txt"
    path.write_text("cataloged twice", encoding="utf-8")
    candidate = ExistingDownload.capture(path)
    assert candidate is not None
    original = database.adopt_subject_file(candidate, subject.id, "Outros")
    assert database.unregister_adopted_file(
        original.id,
        expected_path=original.current_path,
        expected_record_token=original.record_token,
        reviewed_reason=FindingReason.UNTRACKED_SUBJECT_FILE.value,
    )
    replacement = database.adopt_subject_file(candidate, subject.id, "Outros")
    assert replacement.id == original.id
    assert replacement.record_token != original.record_token

    database.replace_document_pages(
        original.id,
        subject.name,
        original.original_name,
        ("stale same-path contents",),
        expected_path=original.current_path,
        expected_record_token=original.record_token,
    )

    refreshed = database.get_file(replacement.id)
    assert refreshed is not None
    assert refreshed.indexed_at is None
    assert database.search("stale") == []


def test_newer_database_version_is_refused_without_downgrade(database: Database) -> None:
    with database.connect() as connection:
        connection.execute("PRAGMA user_version = 6")
        connection.commit()

    with pytest.raises(RuntimeError, match="versão mais recente"):
        database.initialize()

    with database.connect() as connection:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    assert version == 6


def test_normal_filing_reuses_a_dropped_path_tombstone(
    database: Database, subject: Subject, tmp_path: Path
) -> None:
    file_id = _file_record(database, subject, tmp_path / "first")
    original = database.get_file(file_id)
    assert original is not None
    original.current_path.unlink()
    assert database.drop_file_record(
        original.id,
        expected_path=original.current_path,
        expected_origin=original.origin,
        expected_record_token=original.record_token,
        verify_missing=lambda: True,
    )
    new_inbox_path = tmp_path / "second" / "inbox" / "aula.txt"
    new_inbox_path.parent.mkdir(parents=True)
    new_inbox_path.write_text("new document at the same path", encoding="utf-8")
    new_item = database.add_inbox_item(
        new_inbox_path,
        tmp_path / "second" / "downloads" / "aula.txt",
        new_inbox_path.name,
        new_inbox_path.stat().st_size,
    )
    original.current_path.write_text("new document at the same path", encoding="utf-8")

    replacement = database.record_filing(
        new_item.id,
        subject.id,
        "Slides",
        original.current_path,
    )

    assert replacement.id == original.id
    assert replacement.catalog_state == "active"
    assert replacement.record_token != original.record_token
    assert database.latest_undoable_filing() is not None


def test_activity_summary_tracks_lifetime_history(
    database: Database, subject: Subject, tmp_path: Path
) -> None:
    assert database.activity_summary() == ActivitySummary(0, 0, 0, 0, 0, 0)

    file_id = _file_record(database, subject, tmp_path)
    document = database.get_file(file_id)
    assert document is not None
    event = database.latest_undoable_filing()
    assert event is not None
    database.mark_filing_undone(event, tmp_path / "restored.txt")
    adopted_path = tmp_path / "adotado.txt"
    adopted_path.write_text("cataloged in place", encoding="utf-8")
    candidate = ExistingDownload.capture(adopted_path)
    assert candidate is not None
    database.adopt_subject_file(candidate, subject.id, "Outros")
    return_path = tmp_path / "devolvido.txt"
    item = database.add_inbox_item(
        return_path,
        tmp_path / "downloads" / "devolvido.txt",
        return_path.name,
        10,
    )
    database.record_return(item.id, tmp_path / "Downloads" / "devolvido.txt")
    database.increment_metric(METRIC_COLLISIONS_RENAMED)
    database.increment_metric(METRIC_COLLISIONS_RENAMED)

    summary = database.activity_summary()

    assert summary.organized == 1
    assert summary.undone == 1
    assert summary.adopted == 1
    assert summary.returned == 1
    assert summary.collisions_renamed == 2
    assert summary.operations_recovered == 0
