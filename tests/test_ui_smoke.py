"""Headless construction tests for the primary Qt surfaces."""

from __future__ import annotations

import time
from datetime import date, timedelta
from pathlib import Path

import pytest
from PySide6.QtCore import QDate
from PySide6.QtGui import QCursor, QFont, QGuiApplication, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QDialog,
    QLabel,
    QMessageBox,
    QPushButton,
)

from organizador import __version__
from organizador.classifier import guess_filing
from organizador.config import AppConfig
from organizador.controller import AppController
from organizador.db import Database
from organizador.filer import FilingService
from organizador.models import FindingReason, Subject
from organizador.paths import IncompleteMoveError
from organizador.reconcile import findings, visible_findings
from organizador.reconcile import scan as scan_reconciliation
from organizador.ui.dialogs import (
    OnboardingDialog,
    SubjectDialog,
    SubjectFilesDialog,
    TaskDialog,
)
from organizador.ui.main_window import MainWindow
from organizador.ui.pages import SettingsPayload
from organizador.ui.prompt import FilingPrompt
from organizador.ui.theme import apply_theme, get_theme
from organizador.ui.widgets import EmptyState


def test_main_window_builds_and_refreshes_all_pages(
    qt_app: QApplication,
    app_config: AppConfig,
    database: Database,
    subject: Subject,
) -> None:
    apply_theme(qt_app, get_theme("escuro"))
    window = MainWindow(database, app_config)

    window.refresh_all(watching=True, paused=False)
    window.show_page("disciplinas")
    qt_app.processEvents()

    assert window.stack.currentWidget() is window.subjects_page
    assert window.status_label.text() == "A vigiar Downloads"
    assert (
        qt_app.palette().color(QPalette.ColorRole.Window).name()
        == get_theme("escuro").canvas.casefold()
    )
    visible_copy = [item.text() for item in window.subjects_page.findChildren(QLabel)]
    assert any(subject.name in text for text in visible_copy)
    settings_copy = [item.text() for item in window.settings_page.findChildren(QLabel)]
    assert any(f"Organizador v{__version__}" in text for text in settings_copy)
    import_requests: list[bool] = []
    window.inbox_page.import_existing_requested.connect(lambda: import_requests.append(True))
    window.inbox_page.import_button.click()
    assert import_requests == [True]
    window.allow_close = True
    window.close()


def test_settings_preserve_an_exact_minimum_file_size(
    qt_app: QApplication,
    app_config: AppConfig,
    database: Database,
) -> None:
    del qt_app
    app_config.minimum_file_size = 1537
    window = MainWindow(database, app_config)
    payloads: list[SettingsPayload] = []
    window.settings_page.save_requested.connect(payloads.append)

    save = next(
        control
        for control in window.settings_page.findChildren(QPushButton)
        if control.text() == "Guardar definições"
    )
    save.click()

    assert payloads[0]["minimum_file_size"] == 1537
    window.allow_close = True
    window.close()


def test_recovery_row_offers_only_a_safe_folder_action(
    qt_app: QApplication,
    app_config: AppConfig,
    database: Database,
    subject: Subject,
) -> None:
    del subject
    missing_path = app_config.inbox_dir / "em-recuperacao.pdf"
    item = database.add_inbox_item(
        missing_path,
        app_config.downloads_dir / missing_path.name,
        missing_path.name,
        200,
    )
    assert database.mark_inbox_recovery_required(item.id, "Recuperação necessária")
    window = MainWindow(database, app_config)
    opened: list[object] = []
    window.inbox_page.open_path.connect(opened.append)

    window.inbox_page.refresh()
    buttons = window.inbox_page.findChildren(QPushButton)
    button_texts = {control.text() for control in buttons}

    assert window.inbox_page.summary_label.text() == "1 ficheiro precisa de recuperação manual"
    assert "Abrir Universidade" in button_texts
    assert "Organizar" not in button_texts
    assert "Não é da universidade" not in button_texts
    next(control for control in buttons if control.text() == "Abrir Universidade").click()
    assert opened == [app_config.university_root]
    window.allow_close = True
    window.close()


def test_unresolved_history_path_is_visible_in_the_inbox(
    qt_app: QApplication,
    app_config: AppConfig,
    database: Database,
    subject: Subject,
) -> None:
    untracked = app_config.university_root / subject.folder_name / "Slides" / "sem-registo.pdf"
    untracked.write_bytes(b"untracked but untouched")
    window = MainWindow(database, app_config)
    adopted: list[object] = []
    reviewed: list[object] = []
    window.inbox_page.adopt_requested.connect(adopted.append)
    window.inbox_page.dismiss_finding_requested.connect(reviewed.append)
    window.inbox_page.set_reconciliation_report(scan_reconciliation(app_config, database))

    window.inbox_page.refresh()
    visible_copy = [item.text() for item in window.inbox_page.findChildren(QLabel)]
    buttons = window.inbox_page.findChildren(QPushButton)

    assert "1 ocorrência do histórico precisa de revisão" in window.inbox_page.summary_label.text()
    assert any("Encontrado numa disciplina sem registo" in text for text in visible_copy)
    assert any(str(untracked) == text for text in visible_copy)
    next(control for control in buttons if control.text() == "Adotar").click()
    next(control for control in buttons if control.text() == "Marcar revisto").click()
    assert len(adopted) == 1
    assert len(reviewed) == 1
    assert untracked.read_bytes() == b"untracked but untouched"
    window.allow_close = True
    window.close()


def test_two_reconciliation_reasons_at_one_path_render_as_two_findings(
    qt_app: QApplication,
    app_config: AppConfig,
    database: Database,
    filer: FilingService,
    subject: Subject,
) -> None:
    source = app_config.downloads_dir / "same-path.txt"
    source.write_bytes(b"one path with two reasons")
    item = filer.ingest(source)
    assert item is not None
    document = filer.file_document(item.id, subject.id, "Outros", source.name)
    document.current_path.unlink()
    window = MainWindow(database, app_config)
    window.inbox_page.set_reconciliation_report(scan_reconciliation(app_config, database))

    window.inbox_page.refresh()
    visible_copy = [item.text() for item in window.inbox_page.findChildren(QLabel)]
    button_copy = [item.text() for item in window.inbox_page.findChildren(QPushButton)]

    assert (
        "2 ocorrências do histórico precisam de revisão" in window.inbox_page.summary_label.text()
    )
    assert any("Documento registado" in text for text in visible_copy)
    assert any("não pode ser desfeita" in text for text in visible_copy)
    assert visible_copy.count(str(document.current_path)) == 2
    assert "Remover registo" in button_copy
    assert button_copy.count("Marcar revisto") == 2
    window.allow_close = True
    window.close()


def test_controller_adopts_and_unregisters_without_moving_the_file(
    qt_app: QApplication,
    app_config: AppConfig,
    database: Database,
    subject: Subject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del database
    path = app_config.university_root / subject.folder_name / "Outros" / "legacy.txt"
    contents = b"catalog this file in place"
    path.write_bytes(contents)
    controller = AppController(app_config)
    report = scan_reconciliation(app_config, controller.database)
    finding = next(
        item for item in findings(report) if item.reason is FindingReason.UNTRACKED_SUBJECT_FILE
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )

    controller._adopt_untracked_file(finding)
    adopted = controller.database.list_adopted_files()

    assert len(adopted) == 1
    assert path.read_bytes() == contents
    controller._unregister_adopted_file(adopted[0].id)
    report = scan_reconciliation(app_config, controller.database)
    assert controller.database.list_adopted_files() == []
    assert visible_findings(controller.database, report) == ()
    assert path.read_bytes() == contents
    qt_app.processEvents()
    controller.indexer.shutdown()
    controller.tray.hide()
    controller.main_window.allow_close = True
    controller.main_window.close()


def test_home_activity_panel_reports_safety_history(
    qt_app: QApplication,
    app_config: AppConfig,
    database: Database,
    subject: Subject,
) -> None:
    inbox_path = app_config.inbox_dir / "MAT101_historico.pdf"
    inbox_path.write_bytes(b"activity history" * 20)
    item = database.add_inbox_item(
        inbox_path,
        app_config.downloads_dir / inbox_path.name,
        inbox_path.name,
        inbox_path.stat().st_size,
    )
    existing = app_config.university_root / subject.folder_name / "Slides" / "MAT101_historico.pdf"
    existing.write_bytes(b"already filed here")
    filer = FilingService(app_config, database)
    filer.file_document(item.id, subject.id, "Slides", "MAT101_historico.pdf")
    window = MainWindow(database, app_config)

    window.refresh_all(watching=True, paused=False)

    text = window.home_page.activity_label.text()
    assert "1 ficheiro organizado" in text
    assert "1 colisão de nomes resolvida" in text
    window.allow_close = True
    window.close()


def test_home_activity_panel_starts_with_a_calm_empty_state(
    qt_app: QApplication,
    app_config: AppConfig,
    database: Database,
) -> None:
    window = MainWindow(database, app_config)

    window.refresh_all(watching=True, paused=False)

    assert "ainda não tem histórico" in window.home_page.activity_label.text()
    window.allow_close = True
    window.close()


def test_stale_watcher_generation_cannot_ingest_after_restart(
    qt_app: QApplication,
    app_config: AppConfig,
    database: Database,
) -> None:
    del qt_app, database
    controller = AppController(app_config)
    controller._watcher_generation = 2
    candidate = app_config.downloads_dir / "stale-callback.pdf"
    contents = b"stale watcher callback must be ignored"
    candidate.write_bytes(contents)

    controller._ingest_download(1, candidate)

    assert controller.database.count_inbox_items() == 0
    assert candidate.read_bytes() == contents
    controller.indexer.shutdown()
    controller.tray.hide()
    controller.main_window.allow_close = True
    controller.main_window.close()


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

    assert "color: #08111d" in dialog.color_button.styleSheet()
    dialog.color = "#08111D"
    dialog._update_color_button()
    assert "color: #ffffff" in dialog.color_button.styleSheet().casefold()
    dialog.close()


def test_empty_state_reserves_height_for_wrapped_body(qt_app: QApplication) -> None:
    apply_theme(qt_app, get_theme("escuro"))
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
        "filename_template": "{nome_original}",
        "minimum_file_size": 1024,
        "prompt_timeout_seconds": 45,
        "reminder_lead_days": 2,
        "theme": "escuro",
        "language": "pt",
        "check_updates_on_launch": True,
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


def test_startup_reconciles_before_watcher_and_indexer(
    qt_app: QApplication,
    app_config: AppConfig,
    database: Database,
    subject: Subject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del database, subject
    orphan = app_config.inbox_dir / "MAT101_arranque.pdf"
    orphan.write_bytes(b"startup orphan university document")
    controller = AppController(app_config)
    order: list[str] = []

    def observe_watcher_start() -> None:
        recovered = controller.database.find_active_inbox_by_path(orphan)
        assert recovered is not None
        assert recovered.status == "pending"
        order.append("watcher")

    monkeypatch.setattr(controller, "_restart_watcher", observe_watcher_start)
    monkeypatch.setattr(controller.indexer, "submit_pending", lambda: order.append("indexer"))

    controller.start(smoke_test=True)
    qt_app.processEvents()

    recovered = controller.database.find_active_inbox_by_path(orphan)
    assert recovered is not None
    assert recovered.suggested_subject_id is not None
    assert order == ["watcher", "indexer"]
    controller.reminder_timer.stop()
    controller.indexer.shutdown()
    controller.tray.hide()
    controller.main_window.allow_close = True
    controller.main_window.close()


def test_incomplete_return_is_ignored_by_the_live_watcher(
    qt_app: QApplication,
    app_config: AppConfig,
    database: Database,
    subject: Subject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del database, subject
    controller = AppController(app_config)
    source = app_config.downloads_dir / "devolucao-parcial.pdf"
    source.write_bytes(b"return source" * 20)
    item = controller.filer.ingest(source)
    assert item is not None

    class WatcherStub:
        running = True
        paused = False

        def __init__(self) -> None:
            self.ignored: list[tuple[object, float]] = []

        def set_paused(self, paused: bool) -> None:
            self.paused = paused

        def ignore_self_move(self, path: object, *, seconds: float = 30.0) -> None:
            self.ignored.append((path, seconds))

    watcher = WatcherStub()
    controller.watcher = watcher  # type: ignore[assignment]

    def leave_partial(_source: object, target: object, **_kwargs: object) -> object:
        destination = Path(str(target))
        destination.write_bytes(b"partial return copy")
        raise IncompleteMoveError(destination)

    monkeypatch.setattr("organizador.filer.move_without_overwrite", leave_partial)

    controller._return_item(item.id)
    qt_app.processEvents()

    pending = controller.database.list_pending_returns()
    assert len(pending) == 1
    assert watcher.ignored == [(pending[0].destination_path, float("inf"))]
    assert pending[0].destination_path.read_bytes() == b"partial return copy"
    assert item.path.exists()
    controller.indexer.shutdown()
    controller.tray.hide()
    controller.main_window.allow_close = True
    controller.main_window.close()


def test_confirmed_existing_download_import_is_capped_and_uses_normal_inbox_flow(
    qt_app: QApplication,
    app_config: AppConfig,
    database: Database,
    subject: Subject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del database, subject
    app_config.watch_enabled = False
    for index in range(30):
        path = app_config.downloads_dir / f"existing_{index:02}.pdf"
        path.write_bytes(b"existing university material")
    confirmation_copy: list[str] = []
    answers = [QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Yes]

    def confirm(*args: object, **_kwargs: object) -> QMessageBox.StandardButton:
        confirmation_copy.append(str(args[2]))
        return answers.pop(0)

    monkeypatch.setattr(QMessageBox, "question", confirm)
    monkeypatch.setattr("organizador.watcher.wait_until_stable", lambda *_args, **_kwargs: True)
    controller = AppController(app_config)
    controller._restart_watcher()
    controller._import_existing_downloads()
    assert controller.database.count_inbox_items() == 0
    assert len(list(app_config.downloads_dir.glob("*.pdf"))) == 30

    controller._import_existing_downloads()

    deadline = time.monotonic() + 5.0
    while controller._manual_import_active and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.01)
    qt_app.processEvents()

    assert not controller._manual_import_active
    assert controller.database.count_inbox_items() == 25
    assert len(list(app_config.downloads_dir.glob("*.pdf"))) == 5
    assert len(confirmation_copy) == 2
    assert "30 ficheiros elegíveis" in confirmation_copy[1]
    assert "no máximo 25" in confirmation_copy[1]
    assert "25 importados" in controller.main_window.inbox_page.import_status_label.text()
    assert controller.prompt.current_item_id is not None

    if controller.watcher is not None:
        controller.watcher.stop()
    controller.prompt.timer.stop()
    controller.prompt.hide()
    controller.indexer.shutdown()
    controller.tray.hide()
    controller.main_window.allow_close = True
    controller.main_window.close()


def test_advance_reminders_fire_once_per_day_and_survive_restart(
    qt_app: QApplication,
    app_config: AppConfig,
    database: Database,
    subject: Subject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del database
    controller = AppController(app_config)
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(controller.tray, "notify", lambda title, body: sent.append((title, body)))
    soon_id = controller.database.add_task(
        "Entrega breve", subject.id, date.today() + timedelta(days=2)
    ).id
    controller.database.add_task("Longínqua", subject.id, date.today() + timedelta(days=30))

    controller._check_deadlines()
    controller._check_deadlines()

    assert len(sent) == 1
    assert "Entrega breve" in sent[0][1]

    restarted = AppController(app_config)
    restarted_sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        restarted.tray, "notify", lambda title, body: restarted_sent.append((title, body))
    )
    restarted._check_deadlines()
    assert restarted_sent == []

    controller.database.update_task(soon_id, "Entrega breve", subject.id, date.today())
    controller._check_deadlines()

    assert len(sent) == 2
    assert "vence hoje" in sent[1][1]

    controller.reminder_timer.stop()
    restarted.reminder_timer.stop()
    for active in (controller, restarted):
        active.indexer.shutdown()
        active.tray.hide()
        active.main_window.allow_close = True
        active.main_window.close()


def test_per_task_reminder_lead_overrides_the_global_default(
    qt_app: QApplication,
    app_config: AppConfig,
    database: Database,
    subject: Subject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del database
    controller = AppController(app_config)
    sent: list[str] = []
    monkeypatch.setattr(controller.tray, "notify", lambda title, body: sent.append(body))
    controller.database.add_task(
        "Projeto final", subject.id, date.today() + timedelta(days=5), reminder_lead_days=7
    )
    controller.database.add_task("Normal", subject.id, date.today() + timedelta(days=5))

    controller._check_deadlines()

    assert len(sent) == 1
    assert "Projeto final" in sent[0]

    controller.indexer.shutdown()
    controller.tray.hide()
    controller.main_window.allow_close = True
    controller.main_window.close()


class _StubBulkDialog:
    values: tuple[int, str, bool, date | None] = (0, "Slides", False, None)

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def exec(self) -> QDialog.DialogCode:
        return QDialog.DialogCode.Accepted


def test_bulk_filing_files_selection_and_keeps_failures_pending(
    qt_app: QApplication,
    app_config: AppConfig,
    database: Database,
    subject: Subject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del database
    app_config.watch_enabled = False
    controller = AppController(app_config)
    ids: list[int] = []
    for name in ("lote_um.pdf", "lote_dois.pdf", "lote_tres.pdf"):
        path = app_config.inbox_dir / name
        path.write_bytes(b"bulk content" * 10)
        item = controller.database.add_inbox_item(
            path, app_config.downloads_dir / name, name, path.stat().st_size
        )
        ids.append(item.id)
    (app_config.inbox_dir / "lote_tres.pdf").unlink()
    _StubBulkDialog.values = (subject.id, "Slides", True, date(2026, 12, 1))
    monkeypatch.setattr("organizador.controller.BulkFilingDialog", _StubBulkDialog)
    controller.prompt_queue.append(ids[1])

    controller._organise_selection(tuple(ids))

    with controller.database.connect() as connection:
        filed_count = int(
            connection.execute("SELECT COUNT(*) FROM events WHERE action = 'file'").fetchone()[0]
        )
    assert filed_count == 2
    assert controller.database.count_inbox_items() == 1
    pending = controller.database.get_inbox_item(ids[2])
    assert pending is not None
    assert pending.status == "pending"
    assert ids[1] not in controller.prompt_queue
    assert len(controller.database.list_tasks()) == 2
    status = controller.main_window.inbox_page.import_status_label.text()
    assert "2 organizados" in status
    assert "1 com erro" in status

    controller.indexer.shutdown()
    controller.tray.hide()
    controller.main_window.allow_close = True
    controller.main_window.close()


def test_subjects_page_lists_archived_subjects_and_offers_restore(
    qt_app: QApplication,
    app_config: AppConfig,
    database: Database,
    subject: Subject,
) -> None:
    database.archive_subject(subject.id)
    window = MainWindow(database, app_config)
    page = window.subjects_page

    page.archived_check.setChecked(True)

    buttons = page.findChildren(QPushButton)
    labels = [item.text() for item in page.findChildren(QLabel)]
    restore = next(control for control in buttons if control.text() == "Restaurar")
    requests: list[int] = []
    page.restore_requested.connect(requests.append)
    restore.click()

    assert requests == [subject.id]
    assert "Arquivada" in labels
    assert "Arquivar" not in {control.text() for control in buttons}
    window.allow_close = True
    window.close()


def test_task_dialog_prefills_and_returns_values(
    qt_app: QApplication, database: Database, subject: Subject
) -> None:
    task = database.add_task(
        "Estudar integrais", subject.id, date(2026, 12, 10), reminder_lead_days=3
    )

    dialog = TaskDialog(task, [subject])

    assert dialog.title_edit.text() == "Estudar integrais"
    assert dialog.subject_combo.currentData() == subject.id
    assert dialog.values == (
        "Estudar integrais",
        subject.id,
        date(2026, 12, 10),
        3,
    )


def test_inbox_rows_without_recovery_are_selectable_and_pruned(
    qt_app: QApplication,
    app_config: AppConfig,
    database: Database,
    subject: Subject,
) -> None:
    path = app_config.inbox_dir / "selecionavel.pdf"
    path.write_bytes(b"select me")
    database.add_inbox_item(path, app_config.downloads_dir / path.name, path.name, 100)
    missing = app_config.inbox_dir / "fantasma.pdf"
    database.add_inbox_item(missing, app_config.downloads_dir / missing.name, missing.name, 100)
    window = MainWindow(database, app_config)

    window.inbox_page._selected_ids.add(99999)
    window.inbox_page.refresh()
    checkboxes = window.inbox_page.findChildren(QCheckBox)

    assert 99999 not in window.inbox_page._selected_ids
    assert len(checkboxes) == 2
    window.allow_close = True
    window.close()


def test_filing_prompt_prefills_from_the_name_template(
    qt_app: QApplication,
    app_config: AppConfig,
    database: Database,
    subject: Subject,
) -> None:
    path = app_config.inbox_dir / "MAT101_aula5.pdf"
    path.write_bytes(b"x")
    item = database.add_inbox_item(
        path, app_config.downloads_dir / path.name, path.name, path.stat().st_size
    )
    guess = guess_filing(item.original_name, [subject])
    prompt = FilingPrompt(timeout_seconds=30)

    prompt.show_item(item, [subject], guess)
    assert prompt.name_edit.text() == "MAT101_aula5.pdf"

    prompt.show_item(item, [subject], guess, name_template="{codigo} - {nome_original}")
    assert prompt.name_edit.text() == "MAT101 - MAT101_aula5.pdf"

    prompt.timer.stop()
    prompt.hide()


def test_tasks_page_calendar_marks_days_by_deadline_state(
    qt_app: QApplication,
    app_config: AppConfig,
    database: Database,
    subject: Subject,
) -> None:
    today = date.today()
    overdue = database.add_task("Atrasada", subject.id, today.replace(day=1))
    today_task = database.add_task("Hoje", subject.id, today)
    future = database.add_task("Futura", subject.id, today.replace(day=27))
    completed = database.add_task("Feita", subject.id, today.replace(day=12))
    database.set_task_completed(completed.id, True)
    window = MainWindow(database, app_config)
    page = window.tasks_page
    open_formats = {
        day: page.calendar.dateTextFormat(QDate(day.year, day.month, day.day))
        for day in (overdue.due_date, today_task.due_date, future.due_date)
    }

    assert open_formats[overdue.due_date].fontWeight() == QFont.Weight.Bold
    assert open_formats[today_task.due_date].fontWeight() == QFont.Weight.Bold
    assert open_formats[future.due_date].fontWeight() == QFont.Weight.Bold
    assert open_formats[overdue.due_date].foreground().color().name() == "#ff818b"
    assert open_formats[today_task.due_date].foreground().color().name() == "#f1bb68"
    assert open_formats[future.due_date].foreground().color().name() == "#49cfc0"
    assert open_formats[overdue.due_date].background().color().name() == "#4a262e"
    assert open_formats[today_task.due_date].background().color().name() == "#4a3520"
    assert open_formats[future.due_date].background().color().name() == "#1e4d46"
    completed_format = page.calendar.dateTextFormat(
        QDate(completed.due_date.year, completed.due_date.month, completed.due_date.day)
    )
    assert completed_format.fontWeight() != QFont.Weight.Bold
    assert completed_format.foreground().color().name() == "#9baabd"
    assert completed_format.background().color().name() == "#222f3e"
    window.allow_close = True
    window.close()


def test_tasks_page_calendar_click_filters_and_toggle_clears(
    qt_app: QApplication,
    app_config: AppConfig,
    database: Database,
    subject: Subject,
) -> None:
    today = date.today()
    database.add_task("Um", subject.id, today)
    database.add_task("Dois", subject.id, today)
    database.add_task("Outro dia", subject.id, today.replace(day=15))
    window = MainWindow(database, app_config)
    page = window.tasks_page

    page.calendar.clicked.emit(QDate(today.year, today.month, today.day))

    assert page._selected_date == today
    assert not page.clear_filter_button.isHidden()
    page.refresh()
    titles = [label_.text() for label_ in page.findChildren(QLabel)]
    assert "Um" in titles
    assert "Dois" in titles
    assert "Outro dia" not in titles

    page.calendar.clicked.emit(QDate(today.year, today.month, today.day))

    assert page._selected_date is None
    window.allow_close = True
    window.close()


def test_tasks_page_calendar_activation_prefills_the_deadline(
    qt_app: QApplication,
    app_config: AppConfig,
    database: Database,
    subject: Subject,
) -> None:
    chosen = date.today().replace(day=20)
    window = MainWindow(database, app_config)
    page = window.tasks_page
    page.due_check.setChecked(False)

    page.calendar.activated.emit(QDate(chosen.year, chosen.month, chosen.day))

    assert page.due_check.isChecked()
    assert page.due_edit.date() == QDate(chosen.year, chosen.month, chosen.day)
    window.allow_close = True
    window.close()


def test_subjects_page_shows_counts_and_opens_files_overview(
    qt_app: QApplication,
    app_config: AppConfig,
    database: Database,
    subject: Subject,
    filer: FilingService,
) -> None:
    download = app_config.downloads_dir / "revolucao.pdf"
    download.write_bytes(b"material historico" * 12)
    item = filer.ingest(download)
    assert item is not None
    document = filer.file_document(item.id, subject.id, "Slides", download.name)
    database.add_subject("Sem ficheiros", "", "#123456", (), "SEM - Sem ficheiros")
    window = MainWindow(database, app_config)
    page = window.subjects_page
    row_labels = [text.text() for text in page.findChildren(QLabel)]
    assert any("1 ficheiro ·" in text for text in row_labels)
    assert any("Ainda sem ficheiros organizados" in text for text in row_labels)

    requests: list[int] = []
    page.view_files_requested.connect(requests.append)
    view_button = next(
        control for control in page.findChildren(QPushButton) if control.text() == "Ver ficheiros"
    )
    view_button.click()
    assert requests == [subject.id]

    folder_path = app_config.university_root / subject.folder_name
    dialog = SubjectFilesDialog(subject, [document], folder_path)
    opened: list[object] = []
    dialog.open_requested.connect(opened.append)
    dialog_labels = [text.text() for text in dialog.findChildren(QLabel)]
    assert document.current_path.name in dialog_labels
    assert any("1 ficheiro ·" in text for text in dialog_labels)
    assert any("Slides 1" in text for text in dialog_labels)
    dialog_buttons = dialog.findChildren(QPushButton)
    next(control for control in dialog_buttons if control.text() == "Abrir").click()
    next(control for control in dialog_buttons if control.text() == "Abrir pasta").click()
    assert opened == [document.current_path, folder_path]

    empty_dialog = SubjectFilesDialog(subject, [], folder_path)
    empty_labels = [text.text() for text in empty_dialog.findChildren(QLabel)]
    assert any("Ainda não há ficheiros organizados" in text for text in empty_labels)
    window.allow_close = True
    window.close()


def test_controller_view_subject_files_opens_dialog_without_touching_files(
    qt_app: QApplication,
    app_config: AppConfig,
    database: Database,
    subject: Subject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del database, qt_app
    controller = AppController(app_config)
    shown: list[list[object]] = []

    def capture_exec(dialog: object) -> QDialog.DialogCode:
        documents = getattr(dialog, "documents", ())
        shown.append(list(documents))
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(SubjectFilesDialog, "exec", capture_exec)
    controller._view_subject_files(subject.id)

    assert shown == [[]]
    controller.indexer.shutdown()
    controller.tray.hide()
    controller.main_window.allow_close = True
    controller.main_window.close()
