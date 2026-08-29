"""End-to-end file movement tests in temporary folders."""

from __future__ import annotations

from pathlib import Path
from typing import NoReturn

import pytest

from organizador.config import AppConfig
from organizador.db import Database
from organizador.filer import FilingError, FilingService
from organizador.models import FilingHint, Subject


def _download(config: AppConfig, name: str = "MAT101_ficha.pdf", size: int = 200) -> Path:
    path = config.downloads_dir / name
    path.write_bytes(b"x" * size)
    return path


def test_ingest_and_file_document_are_collision_safe(
    app_config: AppConfig,
    database: Database,
    filer: FilingService,
    subject: Subject,
) -> None:
    source = _download(app_config)
    item = filer.ingest(source)
    assert item is not None
    assert not source.exists()
    assert item.path.parent == app_config.inbox_dir

    target_folder = app_config.university_root / subject.folder_name / "Exercícios"
    (target_folder / "Ficha.pdf").write_bytes(b"existing")
    document = filer.file_document(item.id, subject.id, "Exercícios", "Ficha.pdf")

    assert document.current_path.name == "Ficha (2).pdf"
    assert document.current_path.read_bytes() == b"x" * 200
    assert database.count_inbox_items() == 0
    assert database.filing_hints() == [FilingHint("MAT101_ficha.pdf", subject.id, "Exercícios")]


def test_requested_extension_cannot_change_the_original(
    app_config: AppConfig, filer: FilingService, subject: Subject
) -> None:
    item = filer.ingest(_download(app_config, "notas.pdf"))
    assert item is not None

    document = filer.file_document(item.id, subject.id, "Slides", "Notas.exe")

    assert document.current_path.name == "Notas.pdf"


def test_small_or_unsupported_files_remain_in_downloads(
    app_config: AppConfig, filer: FilingService
) -> None:
    small = _download(app_config, "curto.txt", size=2)
    unsupported = _download(app_config, "programa.exe", size=200)

    assert filer.ingest(small) is None
    assert filer.ingest(unsupported) is None
    assert small.exists()
    assert unsupported.exists()


def test_non_university_file_returns_without_overwrite(
    app_config: AppConfig, filer: FilingService
) -> None:
    item = filer.ingest(_download(app_config, "fatura.pdf"))
    assert item is not None
    (app_config.downloads_dir / "fatura.pdf").write_bytes(b"newer file")

    destination = filer.return_to_downloads(item.id)

    assert destination.name == "fatura (2).pdf"
    assert destination.exists()


def test_undo_restores_latest_document_to_inbox(
    app_config: AppConfig, filer: FilingService, subject: Subject
) -> None:
    item = filer.ingest(_download(app_config, "aula.pdf"))
    assert item is not None
    document = filer.file_document(item.id, subject.id, "Slides", "Aula.pdf")

    restored = filer.undo_latest_filing()

    assert restored is not None
    assert restored.path.exists()
    assert restored.path.parent == app_config.inbox_dir
    assert not document.current_path.exists()


def test_database_failure_rolls_subject_move_back(
    app_config: AppConfig,
    database: Database,
    filer: FilingService,
    subject: Subject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = filer.ingest(_download(app_config, "aula.pdf"))
    assert item is not None

    def fail_record(*_args: object, **_kwargs: object) -> NoReturn:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(database, "record_filing", fail_record)

    with pytest.raises(FilingError, match="revertido"):
        filer.file_document(item.id, subject.id, "Slides", "Aula.pdf")

    assert item.path.exists()
    refreshed = database.get_inbox_item(item.id)
    assert refreshed is not None
    assert refreshed.status == "pending"
