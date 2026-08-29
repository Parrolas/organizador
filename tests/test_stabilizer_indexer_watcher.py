"""Background service boundary tests."""

from __future__ import annotations

import json
from pathlib import Path
from threading import Event

from watchdog.events import FileCreatedEvent, FileMovedEvent

from organizador.config import AppConfig
from organizador.db import Database
from organizador.indexer import DocumentIndexer
from organizador.models import Subject
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
