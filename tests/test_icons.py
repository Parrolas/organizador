"""Application logo asset and icon loading tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from organizador.ui import icons
from organizador.ui.icons import app_icon

ASSETS = Path(__file__).resolve().parent.parent / "assets"


def test_logo_assets_exist() -> None:
    assert (ASSETS / "icon.png").is_file()
    assert (ASSETS / "icon-square.png").is_file()
    assert (ASSETS / "icon.ico").is_file()


def test_app_icon_loads_the_logo(qt_app: QApplication) -> None:
    del qt_app

    assert not app_icon().isNull()


def test_app_icon_falls_back_without_assets(
    qt_app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    del qt_app
    monkeypatch.setattr(icons, "_asset_path", lambda _name: None)

    assert not app_icon().isNull()
