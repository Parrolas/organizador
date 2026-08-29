"""Isolated fixtures that never touch the user's real folders."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from organizador.config import AppConfig
from organizador.db import Database
from organizador.filer import FilingService
from organizador.models import Subject


@pytest.fixture
def app_config(tmp_path: Path) -> AppConfig:
    """Return settings rooted entirely under pytest's temporary folder."""

    downloads = tmp_path / "Downloads"
    university = tmp_path / "Universidade"
    downloads.mkdir()
    config = AppConfig(
        data_dir=tmp_path / "AppData",
        university_root=university,
        downloads_dir=downloads,
        minimum_file_size=10,
        initialized=True,
    )
    config.ensure_directories()
    return config


@pytest.fixture
def database(app_config: AppConfig) -> Database:
    """Return an initialized temporary SQLite repository."""

    result = Database(app_config.database_path)
    result.initialize()
    return result


@pytest.fixture
def filer(app_config: AppConfig, database: Database) -> FilingService:
    """Return the sole file-moving service."""

    return FilingService(app_config, database)


@pytest.fixture
def subject(database: Database, filer: FilingService) -> Subject:
    """Create one representative subject and its type folders."""

    result = database.add_subject(
        "Cálculo I",
        "MAT101",
        "#087A74",
        ("calculo", "derivadas", "integrais"),
        filer.subject_folder_name("Cálculo I", "MAT101"),
    )
    filer.ensure_subject_structure(result)
    return result


@pytest.fixture(scope="session")
def qt_app() -> Iterator[QApplication]:
    """Provide one headless Qt application for widget smoke tests."""

    existing = QApplication.instance()
    application = existing if isinstance(existing, QApplication) else QApplication([])
    yield application
    application.processEvents()
