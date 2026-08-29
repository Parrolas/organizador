"""Configuration persistence and path safety tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from organizador.config import AppConfig, parse_extensions


def test_parse_extensions_normalises_and_deduplicates() -> None:
    assert parse_extensions("PDF, .docx; ipynb  PDF") == (".docx", ".ipynb", ".pdf")


def test_accepts_final_study_files_but_not_browser_temporaries(app_config: AppConfig) -> None:
    assert app_config.accepts(Path("aula.pdf"))
    assert app_config.accepts(Path("notebook.IPYNB"))
    assert not app_config.accepts(Path("aula.pdf.crdownload"))
    assert not app_config.accepts(Path("programa.exe"))


def test_settings_round_trip_preserves_unicode_paths(app_config: AppConfig) -> None:
    app_config.university_root = app_config.university_root / "Época 1"
    app_config.allowed_extensions = (".pdf", ".md")
    app_config.prompt_timeout_seconds = 61
    app_config.save()

    loaded = AppConfig.load(app_config.data_dir)

    assert loaded.university_root == app_config.university_root
    assert loaded.allowed_extensions == (".pdf", ".md")
    assert loaded.prompt_timeout_seconds == 61
    assert loaded.initialized


def test_university_folder_cannot_be_inside_downloads(app_config: AppConfig) -> None:
    app_config.university_root = app_config.downloads_dir / "Universidade"

    with pytest.raises(ValueError, match="não podem coincidir"):
        app_config.validate()


def test_downloads_folder_cannot_be_inside_university(app_config: AppConfig) -> None:
    app_config.downloads_dir = app_config.university_root / "Downloads"

    with pytest.raises(ValueError, match="não podem coincidir"):
        app_config.validate()


def test_invalid_popup_timeout_is_rejected(app_config: AppConfig) -> None:
    app_config.prompt_timeout_seconds = 5

    with pytest.raises(ValueError, match="pelo menos 10"):
        app_config.validate()
