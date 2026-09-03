"""Background service boundary tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
from stat import S_IFLNK
from threading import Event
from time import monotonic, sleep

import pytest
from watchdog.events import FileCreatedEvent, FileMovedEvent

from organizador.config import AppConfig
from organizador.db import Database
from organizador.indexer import MAX_PENDING_INDEX_JOBS, DocumentIndexer, _cap_pages
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


def test_watcher_uses_one_key_for_directory_aliases(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ready = Event()
    candidate = app_config.downloads_dir / "returned.pdf"
    candidate.write_bytes(b"returned by Organizador")
    alias_dir = tmp_path / "downloads-alias"
    alias_path = alias_dir / candidate.name
    original_resolve = Path.resolve

    def resolve_alias(path: Path, strict: bool = False) -> Path:
        if path == alias_dir:
            return app_config.downloads_dir.resolve(strict=strict)
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve_alias)
    monkeypatch.setattr("organizador.watcher.wait_until_stable", lambda *_args, **_kwargs: True)
    watcher = DownloadWatcher(app_config, lambda _path: ready.set())
    watcher.start(observe=False)
    try:
        watcher.ignore_self_move(alias_path)
        watcher.enqueue(candidate)

        assert not ready.wait(0.2)
    finally:
        watcher.stop()


def test_watcher_retries_a_download_after_stabilization_timeout(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = 0
    ready = Event()

    def stabilize(*_args: object, **_kwargs: object) -> bool:
        nonlocal attempts
        attempts += 1
        return attempts > 1

    monkeypatch.setattr("organizador.watcher.wait_until_stable", stabilize)
    watcher = DownloadWatcher(app_config, lambda _path: ready.set(), retry_delays=(0.0,))
    watcher.start(observe=False)
    try:
        candidate = app_config.downloads_dir / "slow-download.pdf"
        candidate.write_bytes(b"eventually stable")
        watcher.enqueue(candidate)

        deadline = monotonic() + 2.0
        while not ready.is_set() and monotonic() < deadline:
            watcher._sweep_once()
            sleep(0.01)

        assert ready.is_set()
        assert attempts == 2
    finally:
        watcher.stop()


def test_stopping_manual_import_reports_unprocessed_candidates(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    stabilization_started = Event()
    completed: list[int] = []

    def wait_for_stop(*_args: object, **kwargs: object) -> bool:
        stabilization_started.set()
        stop_event = kwargs["stop_event"]
        assert isinstance(stop_event, Event)
        stop_event.wait(2.0)
        return False

    monkeypatch.setattr("organizador.watcher.wait_until_stable", wait_for_stop)
    candidate_path = app_config.downloads_dir / "manual.pdf"
    candidate_path.write_bytes(b"manual candidate")
    candidate = ExistingDownload.capture(candidate_path)
    assert candidate is not None
    watcher = DownloadWatcher(app_config, lambda _path: None, completed.append)
    watcher.start(observe=False)

    assert watcher.enqueue_existing([candidate]) == 1
    assert stabilization_started.wait(1.0)
    watcher.stop()

    assert completed == [1]
    assert not watcher.manual_import_running


def test_failed_sweep_preserves_the_existing_download_baseline(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = app_config.downloads_dir / "already-present.pdf"
    existing.write_bytes(b"existing download must stay put")
    ready: list[Path | ExistingDownload] = []
    watcher = DownloadWatcher(app_config, ready.append)
    baseline = watcher._snapshot()
    assert baseline is not None
    snapshots = iter((None, baseline))
    monkeypatch.setattr(watcher, "_snapshot", lambda: next(snapshots))
    monkeypatch.setattr("organizador.watcher.wait_until_stable", lambda *_args, **_kwargs: True)
    watcher.start(observe=False)
    try:
        watcher._known = set(baseline)
        watcher._sweep_once()
        watcher._sweep_once()
        sleep(0.1)

        assert ready == []
        assert existing.read_bytes() == b"existing download must stay put"
    finally:
        watcher.stop()


def test_stopping_watcher_suppresses_a_late_successful_stabilization(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    stabilization_started = Event()
    ready = Event()

    def finish_after_stop(*_args: object, **kwargs: object) -> bool:
        stabilization_started.set()
        stop_event = kwargs["stop_event"]
        assert isinstance(stop_event, Event)
        stop_event.wait(2.0)
        return True

    monkeypatch.setattr("organizador.watcher.wait_until_stable", finish_after_stop)
    candidate = app_config.downloads_dir / "late-success.pdf"
    candidate.write_bytes(b"late stabilization result")
    watcher = DownloadWatcher(app_config, lambda _path: ready.set())
    watcher.start(observe=False)
    watcher.enqueue(candidate)
    assert stabilization_started.wait(1.0)

    watcher.stop()

    assert not ready.is_set()
    assert candidate.exists()


def test_exhausted_retry_budget_resets_only_after_file_identity_changes(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = 0
    attempted = Event()

    def never_stable(*_args: object, **_kwargs: object) -> bool:
        nonlocal attempts
        attempts += 1
        attempted.set()
        return False

    monkeypatch.setattr("organizador.watcher.wait_until_stable", never_stable)
    candidate = app_config.downloads_dir / "never-stable.pdf"
    candidate.write_bytes(b"unchanged exhausted candidate")
    watcher = DownloadWatcher(app_config, lambda _path: None, retry_delays=())
    watcher.start(observe=False)
    try:
        watcher.enqueue(candidate)
        assert attempted.wait(1.0)
        attempted.clear()
        watcher.enqueue(candidate)
        assert not attempted.wait(0.2)
        assert attempts == 1

        candidate.write_bytes(b"replacement candidate with a new identity and size")
        watcher.enqueue(candidate)
        assert attempted.wait(1.0)
        assert attempts == 2
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


def test_failed_indexing_batch_does_not_starve_later_documents(
    database: Database, subject: Subject, tmp_path: Path
) -> None:
    for index in range(50):
        malformed = tmp_path / f"malformed-{index:02}.ipynb"
        malformed.write_text("not valid JSON", encoding="utf-8")
        _file_record(database, subject, malformed)
    healthy = tmp_path / "after-malformed.txt"
    healthy.write_text("healthy document after malformed batch", encoding="utf-8")
    healthy_id = _file_record(database, subject, healthy)
    indexer: DocumentIndexer

    def refill(_file_id: int, _error: str) -> None:
        indexer.submit_pending()

    indexer = DocumentIndexer(database, refill)
    try:
        indexer.submit_pending()
        deadline = monotonic() + 5.0
        while not database.search("healthy") and monotonic() < deadline:
            sleep(0.01)

        results = database.search("healthy")
        assert results
        assert results[0].file_id == healthy_id
    finally:
        indexer.shutdown()


def test_index_refill_keeps_a_fixed_outstanding_work_cap(
    database: Database,
    subject: Subject,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for index in range(MAX_PENDING_INDEX_JOBS + 10):
        path = tmp_path / f"queued-{index:02}.txt"
        path.write_text("queued document", encoding="utf-8")
        _file_record(database, subject, path)
    started = Event()
    release = Event()
    indexer = DocumentIndexer(database)

    def block_indexing(_document: object) -> None:
        started.set()
        release.wait(2.0)

    monkeypatch.setattr(indexer, "index_document", block_indexing)
    try:
        indexer.submit_pending()
        assert started.wait(1.0)
        for _ in range(5):
            indexer.submit_pending()

        assert len(indexer._active) == MAX_PENDING_INDEX_JOBS
    finally:
        release.set()
        indexer.shutdown()


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


def test_cap_pages_truncates_at_the_character_limit() -> None:
    kept, truncated = _cap_pages(["ab", "cdef", "gh"], limit=5)

    assert kept == ["ab", "cde"]
    assert truncated is True

    kept, truncated = _cap_pages(["ab", "cd"], limit=4)

    assert kept == ["ab", "cd"]
    assert truncated is False

    with pytest.raises(ValueError):
        _cap_pages(["ab"], limit=0)


def test_changed_file_size_refreshes_the_record_and_defers_indexing(
    database: Database, subject: Subject, tmp_path: Path
) -> None:
    path = tmp_path / "editado.txt"
    path.write_bytes(b"x")
    file_id = _file_record(database, subject, path)
    path.write_bytes(b"y" * 5000)
    indexer = DocumentIndexer(database)

    indexer.index_document(database.get_file(file_id))
    indexer.shutdown()

    refreshed = database.get_file(file_id)
    assert refreshed is not None
    assert refreshed.size == 5000
    assert refreshed.indexed_at is None

    indexer = DocumentIndexer(database)
    indexer.index_document(refreshed)
    indexer.shutdown()

    indexed = database.get_file(file_id)
    assert indexed is not None
    assert indexed.indexed_at is not None


def test_indexed_text_is_capped_but_head_terms_stay_searchable(
    database: Database, subject: Subject, tmp_path: Path
) -> None:
    from organizador.indexer import MAX_INDEX_CHARS

    head = "cabecalho-marcador-unico"
    tail = "cauda-marcador-unico"
    path = tmp_path / "longo.txt"
    path.write_text(head + "\n" + ("texto de enchimento\n" * 60000) + tail + "\n", encoding="utf-8")
    assert path.stat().st_size > MAX_INDEX_CHARS
    file_id = _file_record(database, subject, path)
    indexer = DocumentIndexer(database)

    indexer.index_document(database.get_file(file_id))
    indexer.shutdown()

    assert database.search("cabecalho-marcador-unico")
    assert database.search("cauda-marcador-unico") == []
    assert database.search("indexed") == []
