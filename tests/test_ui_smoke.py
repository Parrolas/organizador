"""Headless construction tests for the primary Qt surfaces."""

from __future__ import annotations

import pytest
from PySide6.QtGui import QCursor, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication, QButtonGroup, QLabel

from organizador.classifier import guess_filing
from organizador.config import AppConfig
from organizador.controller import AppController
from organizador.db import Database
from organizador.filer import FilingService
from organizador.models import Subject
from organizador.ui.dialogs import OnboardingDialog, SubjectDialog
from organizador.ui.main_window import MainWindow
from organizador.ui.pages import SettingsPayload
from organizador.ui.prompt import FilingPrompt
from organizador.ui.theme import CANVAS, apply_theme
from organizador.ui.widgets import EmptyState


def test_main_window_builds_and_refreshes_all_pages(
    qt_app: QApplication,
    app_config: AppConfig,
    database: Database,
    subject: Subject,
) -> None:
    apply_theme(qt_app)
    window = MainWindow(database, app_config)

    window.refresh_all(watching=True, paused=False)
    window.show_page("disciplinas")
    qt_app.processEvents()

    assert window.stack.currentWidget() is window.subjects_page
    assert window.status_label.text() == "A vigiar Downloads"
    assert qt_app.palette().color(QPalette.ColorRole.Window).name() == CANVAS.casefold()
    visible_copy = [item.text() for item in window.subjects_page.findChildren(QLabel)]
    assert any(subject.name in text for text in visible_copy)
    window.allow_close = True
    window.close()


def test_filing_prompt_selects_classifier_suggestion(
    qt_app: QApplication,
    app_config: AppConfig,
    database: Database,
    subject: Subject,
) -> None:
    path = app_config.inbox_dir / "MAT101_ficha.pdf"
    path.write_bytes(b"content")
    item = database.add_inbox_item(path, app_config.downloads_dir / path.name, path.name, 7)
    guess = guess_filing(item.original_name, [subject])
    prompt = FilingPrompt(timeout_seconds=30)

    prompt.show_item(item, [subject], guess)
    qt_app.processEvents()

    assert prompt.current_item_id == item.id
    assert prompt.selected_subject_id == subject.id
    assert prompt.confirm_button.isEnabled()
    assert prompt.type_buttons["Exercícios"].isChecked()
    screen = QGuiApplication.screenAt(QCursor.pos()) or prompt.screen()
    target = prompt.animation.endValue()
    assert target.x() == screen.availableGeometry().left() + 18

    prompt.show_item(item, [subject], guess)
    qt_app.processEvents()
    assert len(prompt.findChildren(QButtonGroup)) == 2
    prompt.timer.stop()
    prompt.hide()


def test_subject_colour_button_keeps_readable_text(qt_app: QApplication) -> None:
    dialog = SubjectDialog()

    assert "color: #08111D" in dialog.color_button.styleSheet()
    dialog.color = "#08111D"
    dialog._update_color_button()
    assert "color: #FFFFFF" in dialog.color_button.styleSheet()
    dialog.close()


def test_empty_state_reserves_height_for_wrapped_body(qt_app: QApplication) -> None:
    apply_theme(qt_app)
    empty = EmptyState(
        "Tudo no lugar",
        "Quando terminares um download elegível, ele aparece aqui e num pequeno popup.",
    )
    empty.resize(900, 270)
    empty.show()
    qt_app.processEvents()

    detail = next(
        child for child in empty.findChildren(QLabel) if child.objectName() == "PageSubtitle"
    )
    assert detail.width() == 520
    assert detail.height() >= detail.heightForWidth(detail.width())
    empty.close()


def test_onboarding_removes_subject_when_settings_save_fails(
    qt_app: QApplication,
    app_config: AppConfig,
    database: Database,
    filer: FilingService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_config.initialized = False
    old_root = app_config.university_root
    dialog = OnboardingDialog(app_config, database, filer)
    dialog.root_edit.setText(str(old_root / "Nova"))
    dialog.name_edit.setText("Álgebra Linear")

    def fail_save(_config: AppConfig) -> None:
        raise OSError("disco indisponível")

    monkeypatch.setattr(AppConfig, "save", fail_save)
    dialog._finish()
    qt_app.processEvents()

    assert database.count_subjects() == 0
    assert app_config.university_root == old_root
    assert not app_config.initialized
    assert "disco indisponível" in dialog.error_label.text()
    dialog.close()


def test_startup_registration_is_reverted_when_settings_save_fails(
    qt_app: QApplication,
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = AppController(app_config)
    calls: list[bool] = []
    monkeypatch.setattr("organizador.controller.set_launch_at_login", calls.append)

    def fail_save(_config: AppConfig) -> None:
        raise OSError("sem espaço")

    monkeypatch.setattr(AppConfig, "save", fail_save)
    payload: SettingsPayload = {
        "university_root": app_config.university_root,
        "downloads_dir": app_config.downloads_dir,
        "extensions": ".pdf",
        "minimum_file_size": 1024,
        "prompt_timeout_seconds": 45,
        "watch_enabled": True,
        "launch_at_login": True,
    }

    controller._save_settings(payload)
    qt_app.processEvents()

    assert calls == [True, False]
    assert not app_config.launch_at_login
    assert "sem espaço" in controller.main_window.settings_page.status_label.text()
    controller.indexer.shutdown()
    controller.tray.hide()
    controller.main_window.allow_close = True
    controller.main_window.close()
