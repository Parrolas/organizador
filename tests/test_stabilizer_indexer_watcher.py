"""Background service boundary tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
from stat import S_IFLNK
from threading import Event

import pytest
from watchdog.events import FileCreatedEvent, FileMovedEvent

from organizador.config import AppConfig
from organizador.db import Database
from organizador.indexer import DocumentIndexer
from organizador.models import ExistingDownload, Subject
from organizador.stabilizer import wait_until_stable
from organizador.watcher import DownloadEventHandler, DownloadWatcher


def _file_record(database: Database, subject: Subject, path: Path) -> int:
    inbox = database.add_inbox_item(
        path, path.parent / "original.txt", path.name, path.stat().st_size
    )
    return database.record_filing(inbox.id, subject.id, "Outros", path).id


def test_stabilizer_accepts_unchanged_file_and_rejects_temporary(tmp_path: Path) -> None:
    stable = tmp_path / "aula.pdf"
    stable.write_bytes(b"complete")
    temporary = tmp_path / "aula.pdf.crdownload"
    temporary.write_bytes(b"partial")

    assert wait_until_stable(stable, interval=0.01, stable_samples=1, timeout=0.2)
    assert not wait_until_stable(temporary, interval=0.01, timeout=0.05)


def test_event_handler_uses_created_and_final_moved_path(tmp_path: Path) -> None:
    candidates: list[Path] = []
    handler = DownloadEventHandler(candidates.append)
    created = tmp_path / "notes.pdf"
    final = tmp_path / "slides.pdf"

    handler.on_created(FileCreatedEvent(str(created)))
    handler.on_moved(FileMovedEvent(str(tmp_path / "slides.part"), str(final)))

    assert candidates == [created, final]


def test_native_watcher_detects_a_new_stable_download(app_config: AppConfig) -> None:
    config = app_config
    ready = Event()
    candidates: list[Path] = []

    def received(path: Path) -> None:
        candidates.append(path)
        ready.set()

    watcher = DownloadWatcher(config, received)
    watcher.start()
    try:
        candidate = config.downloads_dir / "nova_aula.pdf"
        candidate.write_bytes(b"completed download")
        assert ready.wait(5.0), "watchdog did not emit the completed download"
        assert candidates == [candidate.resolve()]
    finally:
        watcher.stop()


def test_native_watcher_ignores_a_file_returned_by_the_app(app_config: AppConfig) -> None:
    ready = Event()
    watcher = DownloadWatcher(app_config, lambda _path: ready.set())
    watcher.start()
    try:
        watcher.set_paused(True)
        returned = app_config.downloads_dir / "not_university.pdf"
        returned.write_bytes(b"returned by Organizador")
        watcher.ignore_self_move(returned)
        watcher.set_paused(False)

        assert not ready.wait(3.0), "the watcher re-ingested its own returned file"
    finally:
        watcher.stop()


def test_confirmed_existing_batch_bypasses_pause_and_enforces_cap(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidates: list[ExistingDownload] = []
    for index in range(30):
        path = app_config.downloads_dir / f"existing_{index:02}.pdf"
        path.write_bytes(b"completed existing download")
        candidate = ExistingDownload.capture(path)
        assert candidate is not None
        candidates.append(candidate)
    ready: list[Path | ExistingDownload] = []
    completed = Event()
    skipped: list[int] = []

    def import_completed(count: int) -> None:
        skipped.append(count)
        completed.set()

    monkeypatch.setattr("organizador.watcher.wait_until_stable", lambda *_args, **_kwargs: True)
    watcher = DownloadWatcher(app_config, ready.append, import_completed)
    snapshot_calls: list[bool] = []
    monkeypatch.setattr(watcher, "_snapshot", lambda: snapshot_calls.append(True) or set())
    watcher.start(observe=False)
    watcher.set_paused(True)
    try:
        assert watcher.enqueue_existing(candidates) == 25
        assert completed.wait(3.0), "manual import batch did not complete"
        assert len(ready) == 25
        assert all(isinstance(item, ExistingDownload) for item in ready)
        assert skipped == [0]
        assert not watcher.running
        assert snapshot_calls == []
    finally:
        watcher.stop()


def test_text_and_notebook_indexing_is_searchable(
    database: Database, subject: Subject, tmp_path: Path
) -> None:
    text_path = tmp_path / "cadeia.txt"
    text_path.write_text("A regra da cadeia deriva funções compostas.", encoding="utf-8")
    text_id = _file_record(database, subject, text_path)
    notebook_path = tmp_path / "analise.ipynb"
    notebook_path.write_text(
        json.dumps({"cells": [{"cell_type": "markdown", "source": ["Integrais impróprios"]}]}),
        encoding="utf-8",
    )
    notebook_id = _file_record(database, subject, notebook_path)
    indexer = DocumentIndexer(database)

    text_document = database.get_file(text_id)
    notebook_document = database.get_file(notebook_id)
    assert text_document is not None
    assert notebook_document is not None
    indexer.index_document(text_document)
    indexer.index_document(notebook_document)
    indexer.shutdown()

    assert database.search("composta")[0].file_id == text_id
    assert database.search("improprio")[0].file_id == notebook_id


def test_indexer_rejects_new_work_after_shutdown(
    database: Database, subject: Subject, tmp_path: Path
) -> None:
    path = tmp_path / "por_indexar.txt"
    path.write_text("conteúdo", encoding="utf-8")
    file_id = _file_record(database, subject, path)
    document = database.get_file(file_id)
    assert document is not None
    indexer = DocumentIndexer(database)

    indexer.shutdown()
    indexer.submit(document)

    refreshed = database.get_file(file_id)
    assert refreshed is not None
    assert refreshed.indexed_at is None


def test_missing_document_remains_pending_for_future_indexing(
    database: Database, subject: Subject, tmp_path: Path
) -> None:
    path = tmp_path / "temporariamente-ausente.txt"
    path.write_text("conteúdo recuperável", encoding="utf-8")
    file_id = _file_record(database, subject, path)
    document = database.get_file(file_id)
    assert document is not None
    path.unlink()
    indexer = DocumentIndexer(database)

    indexer.index_document(document)
    indexer.shutdown()

    refreshed = database.get_file(file_id)
    assert refreshed is not None
    assert refreshed.indexed_at is None


def test_missing_documents_do_not_starve_later_indexing_work(
    database: Database, subject: Subject, tmp_path: Path
) -> None:
    for index in range(51):
        missing = tmp_path / f"missing-{index:02}.txt"
        missing.write_text("temporarily missing", encoding="utf-8")
        _file_record(database, subject, missing)
        missing.unlink()
    healthy = tmp_path / "healthy.txt"
    healthy.write_text("ready to index", encoding="utf-8")
    healthy_id = _file_record(database, subject, healthy)

    pending = database.list_unindexed_documents(limit=50)

    assert [document.id for document in pending] == [healthy_id]


def test_symlinked_document_is_never_selected_or_indexed(
    database: Database,
    subject: Subject,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "filed-link.txt"
    path.write_text("original document", encoding="utf-8")
    file_id = _file_record(database, subject, path)
    document = database.get_file(file_id)
    assert document is not None
    original_lstat = Path.lstat
    details = list(path.lstat())
    details[0] = S_IFLNK | 0o777
    symlink_details = os.stat_result(details)

    def report_symlink(candidate: Path) -> os.stat_result:
        return symlink_details if candidate == path else original_lstat(candidate)

    monkeypatch.setattr(Path, "lstat", report_symlink)
    indexer = DocumentIndexer(database)

    assert database.list_unindexed_documents() == []
    indexer.index_document(document)
    indexer.shutdown()

    refreshed = database.get_file(file_id)
    assert refreshed is not None
    assert refreshed.indexed_at is None
    assert database.search("indexed") == []
