"""Application orchestration between background services and Qt widgets."""

from __future__ import annotations

import copy
import logging
import os
import sqlite3
import subprocess
import threading
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QSystemTrayIcon

from organizador import updater
from organizador.classifier import guess_filing
from organizador.config import AppConfig, parse_extensions
from organizador.db import Database
from organizador.filer import FilingError, FilingService, render_final_name
from organizador.i18n import _
from organizador.indexer import DocumentIndexer
from organizador.logging_setup import configure_logging
from organizador.models import (
    ExistingDownload,
    FilingGuess,
    FindingReason,
    InboxItem,
    ReconciliationFinding,
    Subject,
)
from organizador.reconcile import (
    adopt_untracked_subject_file,
    dismiss_finding,
    drop_missing_document,
    unregister_adopted_document,
    visible_findings,
)
from organizador.reconcile import apply as apply_reconciliation
from organizador.reconcile import scan as scan_reconciliation
from organizador.recovery import RecoveryBundle, RecoveryCoordinator
from organizador.startup import ensure_start_menu_shortcut, set_launch_at_login
from organizador.ui.dialogs import (
    BulkFilingDialog,
    OnboardingDialog,
    SubjectDialog,
    SubjectFilesDialog,
)
from organizador.ui.main_window import MainWindow
from organizador.ui.pages import SettingsPayload
from organizador.ui.prompt import FilingPrompt
from organizador.ui.theme import apply_theme, get_theme
from organizador.ui.tray import TrayIcon
from organizador.updater import UpdateCheckResult, UpdateInfo, UpdateTransaction
from organizador.watcher import DownloadWatcher

LOGGER = logging.getLogger(__name__)


class _UpdateInstallAborted(Exception):
    """The update worker stopped early because the application is shutting down."""


@dataclass(frozen=True, slots=True)
class StartupState:
    """Whether onboarding completed and background services may start."""

    configured: bool
    services_ready: bool


def _deadline_copy(delta_days: int) -> str:
    if delta_days < 0:
        return _("está atrasada")
    if delta_days == 0:
        return _("vence hoje")
    if delta_days == 1:
        return _("vence amanhã")
    return _("vence em {count} dias").format(count=delta_days)


class AppController(QObject):
    """Own the long-running services and marshal worker events onto Qt's thread."""

    download_ready = Signal(int, object)
    index_completed = Signal(int, str)
    import_completed = Signal(int)
    update_check_finished = Signal(object, bool, int)
    update_install_finished = Signal(object)

    def __init__(self, config: AppConfig, database: Database | None = None) -> None:
        super().__init__()
        self.config = config
        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        configure_logging(self.config.data_dir)
        if database is None:
            database = Database(config.database_path)
            database.initialize()
        self.database = database
        self.filer = FilingService(config, self.database)
        self.indexer = DocumentIndexer(
            self.database,
            lambda file_id, error: self.index_completed.emit(file_id, error),
        )
        self.watcher: DownloadWatcher | None = None
        self.prompt_queue: deque[int] = deque()
        self.hide_notice_shown = False
        self._manual_import_active = False
        self._manual_imported = 0
        self._manual_import_skipped = 0
        self._manual_import_failed = 0
        self._manual_import_deferred = 0
        self._manual_import_errors: list[str] = []
        self._watcher_generation = 0
        self._pending_update: UpdateInfo | None = None
        self._update_installing = False
        self._update_checking = False
        self._update_check_generation = 0
        self._update_transaction: UpdateTransaction | None = None
        self._update_restart_armed = False
        self._abort_update_install = False

        self.main_window = MainWindow(self.database, config)
        self.prompt = FilingPrompt(config.prompt_timeout_seconds)
        self.tray = TrayIcon(self)
        self._connect_signals()

        self.reminder_timer = QTimer(self)
        self.reminder_timer.setInterval(60_000)
        self.reminder_timer.timeout.connect(self._check_deadlines)

    def start(self, *, background: bool = False, smoke_test: bool = False) -> None:
        """Run first-time setup, start services and reveal the appropriate surface."""

        state = self.prepare(smoke_test=smoke_test)
        self.activate(state, background=background, smoke_test=smoke_test)

    def prepare(self, *, smoke_test: bool = False) -> StartupState:
        """Show the tray and run onboarding plus reconciliation without services."""

        application = QApplication.instance()
        if not isinstance(application, QApplication):  # pragma: no cover - invariant
            raise RuntimeError("QApplication must exist before AppController")
        if not self.tray.available:
            application.setQuitOnLastWindowClosed(True)
            self.main_window.allow_close = True
        else:
            self.tray.show()

        configured = self.config.initialized and self.database.count_subjects() > 0
        if not configured and not smoke_test:
            onboarding = OnboardingDialog(self.config, self.database, self.filer, self.main_window)
            configured = onboarding.exec() == QDialog.DialogCode.Accepted

        services_ready = False
        if configured:
            try:
                self.config.ensure_directories()
            except (ValueError, OSError) as exc:
                QMessageBox.critical(
                    self.main_window,
                    _("Não foi possível abrir as pastas"),
                    _("Revê as Definições antes de ativar a vigilância.\n\n{error}").format(
                        error=exc
                    ),
                )
            else:
                self._reconcile_startup()
                services_ready = True
        return StartupState(configured=configured, services_ready=services_ready)

    def activate(
        self,
        state: StartupState,
        *,
        background: bool = False,
        smoke_test: bool = False,
    ) -> None:
        """Start background services and reveal the appropriate surface."""

        if state.services_ready:
            self._restart_watcher()
            self.indexer.submit_pending()
            self.reminder_timer.start()
            if self.config.check_updates_on_launch:
                self._begin_update_check(automatic=True)
            self._handle_legacy_rollback_bridge()
            self._show_pending_update_result()
            threading.Thread(
                target=ensure_start_menu_shortcut,
                name="start-menu-shortcut",
                daemon=True,
            ).start()

        self._refresh()
        if background and state.configured and not smoke_test:
            self.main_window.hide()
        else:
            self.main_window.show_from_tray("inicio" if state.configured else "disciplinas")

    def _reconcile_startup(self) -> None:
        """Repair safe inbox states before background services inspect the folders."""

        try:
            report = scan_reconciliation(self.config, self.database)
            outcome = apply_reconciliation(self.database, report)
            subjects = self.database.list_subjects()
            for item in outcome.recovered_items:
                guess = self._filing_guess(item.original_name, subjects)
                self.database.update_inbox_suggestion(item.id, guess.subject_id, guess.kind)
            remaining = scan_reconciliation(self.config, self.database)
            manual_paths = {finding.path for finding in visible_findings(self.database, remaining)}
        except sqlite3.Error as exc:
            LOGGER.exception("Startup reconciliation failed")
            self.tray.notify(
                _("Verificação de segurança incompleta"),
                _("Não foi possível rever o histórico local: {error}").format(error=exc),
            )
            return

        self.main_window.inbox_page.set_reconciliation_report(remaining)
        if report.finding_count:
            LOGGER.info(
                "Startup reconciliation found %s issue(s) and changed %s record(s)",
                report.finding_count,
                outcome.change_count,
            )
        for path in sorted(manual_paths, key=lambda value: str(value).casefold()):
            LOGGER.warning("Manual file reconciliation required: %s", path)

        if (
            not outcome.change_count
            and not manual_paths
            and not remaining.truncated
            and not remaining.incomplete
        ):
            return
        details: list[str] = []
        if outcome.change_count:
            details.append(
                (
                    _("{count} registos da Caixa de Entrada foram revistos")
                    if outcome.change_count != 1
                    else _("{count} registo da Caixa de Entrada foi revisto")
                ).format(count=outcome.change_count)
            )
        if manual_paths:
            count = len(manual_paths)
            details.append(
                (
                    _("{count} ficheiros precisam de revisão manual")
                    if count != 1
                    else _("{count} ficheiro precisa de revisão manual")
                ).format(count=count)
            )
        if remaining.truncated:
            details.append(_("a verificação atingiu o limite de segurança"))
        if remaining.incomplete:
            details.append(_("alguns caminhos não puderam ser verificados"))
        self.tray.notify(
            (
                _("Verificação de segurança incompleta")
                if remaining.incomplete
                else _("Verificação de segurança concluída")
            ),
            ". ".join(details) + ". " + _("Abre a Caixa de Entrada para rever."),
        )

    def shutdown(self) -> None:
        """Stop worker threads before terminating Qt."""

        if self._update_installing and not self._update_restart_armed:
            # The helper is not supervising yet; ask the worker to abort cleanly
            # between steps. When the helper already launched, shutdown proceeds
            # and the helper completes the update after this process exits.
            self._abort_update_install = True
        self.reminder_timer.stop()
        self._watcher_generation += 1
        if self.watcher is not None:
            self.watcher.stop()
            self.watcher = None
        self._reset_manual_import_state()
        self.indexer.shutdown()
        self.prompt.hide()
        self.tray.hide()
        self.main_window.allow_close = True
        self.main_window.close()
        QApplication.quit()

    def show_main(self, page: str = "inicio") -> None:
        """Restore the window and refresh current data."""

        self._refresh()
        self.main_window.show_from_tray(page)

    def _connect_signals(self) -> None:
        self.download_ready.connect(self._ingest_download)
        self.index_completed.connect(self._index_finished)
        self.import_completed.connect(self._manual_import_finished)
        self.tray.open_requested.connect(lambda: self.show_main("inicio"))
        self.tray.inbox_requested.connect(lambda: self.show_main("inbox"))
        self.tray.pause_requested.connect(self._set_paused)
        self.tray.undo_requested.connect(self._undo)
        self.tray.settings_requested.connect(lambda: self.show_main("definicoes"))
        self.tray.check_updates_requested.connect(lambda: self._begin_update_check(automatic=False))
        self.tray.install_update_requested.connect(self._install_pending_update)
        self.tray.quit_requested.connect(self.shutdown)
        self.update_check_finished.connect(self._on_update_check_finished)
        self.update_install_finished.connect(self._on_update_install_finished)
        self.main_window.hidden_to_tray.connect(self._hidden_to_tray)

        self.main_window.home_page.open_path.connect(self._open_path)
        self.main_window.home_page.open_university.connect(
            lambda: self._open_path(self.config.university_root)
        )
        self.main_window.home_page.show_inbox.connect(lambda: self.show_main("inbox"))
        self.main_window.inbox_page.organise_requested.connect(self._organise_item)
        self.main_window.inbox_page.organise_selection_requested.connect(self._organise_selection)
        self.main_window.inbox_page.return_requested.connect(self._return_item)
        self.main_window.inbox_page.open_path.connect(self._open_path)
        self.main_window.inbox_page.import_existing_requested.connect(
            self._import_existing_downloads
        )
        self.main_window.inbox_page.adopt_requested.connect(self._adopt_untracked_file)
        self.main_window.inbox_page.drop_record_requested.connect(self._drop_missing_record)
        self.main_window.inbox_page.dismiss_finding_requested.connect(
            self._dismiss_reconciliation_finding
        )
        self.main_window.inbox_page.unregister_requested.connect(self._unregister_adopted_file)
        self.main_window.search_page.open_path.connect(self._open_path)
        self.main_window.search_page.reveal_path.connect(self._reveal_path)
        self.main_window.search_page.retry_requested.connect(self._retry_failed_indexes)
        self.main_window.tasks_page.changed.connect(self._refresh)
        self.main_window.subjects_page.add_requested.connect(self._add_subject)
        self.main_window.subjects_page.edit_requested.connect(self._edit_subject)
        self.main_window.subjects_page.archive_requested.connect(self._archive_subject)
        self.main_window.subjects_page.restore_requested.connect(self._restore_subject)
        self.main_window.subjects_page.view_files_requested.connect(self._view_subject_files)
        self.main_window.subjects_page.open_folder.connect(self._open_path)
        self.main_window.settings_page.save_requested.connect(self._save_settings)

        self.prompt.filing_requested.connect(self._file_item)
        self.prompt.later_requested.connect(self._prompt_finished)
        self.prompt.return_requested.connect(self._return_item)

    def _restart_watcher(self) -> None:
        self._watcher_generation += 1
        generation = self._watcher_generation
        interrupted_import = self._manual_import_active
        if self.watcher is not None:
            self.watcher.stop()
            self.watcher = None
        if interrupted_import:
            self._reset_manual_import_state()
            self.main_window.inbox_page.set_import_status(
                _(
                    "A importação foi interrompida. Os ficheiros ainda não importados "
                    "ficaram em Downloads."
                )
            )
        if not (self.config.initialized and self.database.count_subjects() > 0):
            return
        candidate = DownloadWatcher(
            self.config,
            lambda download: self.download_ready.emit(generation, download),
            lambda skipped: self.import_completed.emit(skipped),
        )
        try:
            candidate.start(observe=self.config.watch_enabled)
        except Exception as exc:
            LOGGER.exception("Could not start the Downloads watcher")
            with suppress(Exception):
                candidate.stop()
            self.watcher = None
            self.tray.notify(
                _("Vigilância de Downloads desligada"),
                _("Não foi possível abrir a pasta configurada: {error}").format(error=exc),
            )
            return
        self.watcher = candidate

    def _ingest_download(
        self,
        generation: int,
        candidate: Path | ExistingDownload,
    ) -> None:
        if generation != self._watcher_generation:
            return
        if isinstance(candidate, ExistingDownload):
            manual_candidate = candidate
            path = candidate.path
        else:
            manual_candidate = None
            path = candidate
        try:
            item = self.filer.ingest(path, expected=manual_candidate)
        except FilingError as exc:
            LOGGER.exception("Could not ingest %s", path)
            if manual_candidate is not None:
                self._manual_import_failed += 1
                self._manual_import_errors.append(f"{path.name}: {exc}")
            else:
                self.tray.notify(_("Não foi possível recolher o ficheiro"), str(exc))
            return
        if item is None:
            if manual_candidate is not None:
                self._manual_import_skipped += 1
            return
        if manual_candidate is not None:
            self._manual_imported += 1
        self._queue_ingested_item(item, notify=manual_candidate is None)

    def _queue_ingested_item(self, item: InboxItem, *, notify: bool) -> None:
        """Classify an ingested item and queue the normal human decision prompt."""

        subjects = self.database.list_subjects()
        guess = self._filing_guess(item.original_name, subjects)
        self.database.update_inbox_suggestion(item.id, guess.subject_id, guess.kind)
        if item.id not in self.prompt_queue and self.prompt.current_item_id != item.id:
            self.prompt_queue.append(item.id)
        if notify:
            self.tray.notify(
                _("Novo material na Caixa de Entrada"),
                _("{name} está pronto para organizar.").format(name=item.original_name),
            )
            self._refresh()
        self._show_next_prompt()

    def _import_existing_downloads(self) -> None:
        watcher = self.watcher
        if watcher is None or not watcher.active:
            QMessageBox.warning(
                self.main_window,
                _("Importação indisponível"),
                _("Conclui a configuração da aplicação antes de importar ficheiros."),
            )
            return
        if self._manual_import_active or watcher.manual_import_running:
            QMessageBox.information(
                self.main_window,
                _("Importação em curso"),
                _("Espera que o lote atual termine antes de iniciar outro."),
            )
            return
        try:
            plan = self.filer.plan_existing_downloads()
        except FilingError as exc:
            QMessageBox.warning(self.main_window, _("Não foi possível procurar"), str(exc))
            return
        if not plan.selected:
            QMessageBox.information(
                self.main_window,
                _("Nada para importar"),
                _("Não existem ficheiros elegíveis no nível principal de Downloads."),
            )
            return

        selected_count = len(plan.selected)
        remaining = plan.total - selected_count
        remaining_copy = (
            _(" Os outros {count} ficam em Downloads para um próximo lote.").format(count=remaining)
            if remaining
            else ""
        )
        answer = QMessageBox.question(
            self.main_window,
            _("Importar ficheiros existentes?"),
            _(
                "Foram encontrados {total} ficheiros elegíveis. "
                "Serão verificados no máximo {selected} e movidos para a Caixa de Entrada."
                "{remaining}\n\nCada ficheiro continuará a precisar da tua confirmação."
            ).format(total=plan.total, selected=selected_count, remaining=remaining_copy),
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self._manual_import_active = True
        self._manual_imported = 0
        self._manual_import_skipped = 0
        self._manual_import_failed = 0
        self._manual_import_deferred = remaining
        self._manual_import_errors.clear()
        self.main_window.inbox_page.set_import_running(True)
        self.main_window.inbox_page.set_import_status(
            _("A verificar {count} ficheiro…").format(count=selected_count)
            if selected_count == 1
            else _("A verificar {count} ficheiros…").format(count=selected_count)
        )
        queued = watcher.enqueue_existing(plan.selected)
        if queued == 0:
            self._reset_manual_import_state()
            self.main_window.inbox_page.set_import_status(
                _("Os ficheiros mudaram ou já estavam a ser processados e ficaram em Downloads.")
            )

    def _manual_import_finished(self, worker_skipped: int) -> None:
        if not self._manual_import_active:
            return
        skipped = self._manual_import_skipped + worker_skipped
        imported = self._manual_imported
        failed = self._manual_import_failed
        parts = [
            _("{count} importado").format(count=imported)
            if imported == 1
            else _("{count} importados").format(count=imported)
        ]
        if skipped:
            parts.append(
                _("{count} ignorado").format(count=skipped)
                if skipped == 1
                else _("{count} ignorados").format(count=skipped)
            )
        if failed:
            parts.append(_("{count} com erro").format(count=failed))
        message = ", ".join(parts) + "."
        if self._manual_import_deferred:
            message += " " + _("{count} não entraram neste lote e ficaram em Downloads.").format(
                count=self._manual_import_deferred
            )
        if self._manual_import_errors:
            shown_errors = "; ".join(self._manual_import_errors[:3])
            if len(self._manual_import_errors) > 3:
                shown_errors += " " + _("e mais {count}").format(
                    count=len(self._manual_import_errors) - 3
                )
            message += " " + _("Revê: {errors}.").format(errors=shown_errors)
        message += " " + _("Nenhum ficheiro foi substituído ou apagado.")
        self._reset_manual_import_state()
        self.main_window.inbox_page.set_import_status(message)
        self._refresh()
        self.tray.notify(_("Importação de Downloads concluída"), message)

    def _adopt_untracked_file(self, finding: ReconciliationFinding) -> None:
        if finding.reason is not FindingReason.UNTRACKED_SUBJECT_FILE:
            return
        answer = QMessageBox.question(
            self.main_window,
            "Adotar ficheiro existente?",
            f"{finding.path.name} será adicionado ao catálogo e à pesquisa local. "
            "O ficheiro não será movido, renomeado nem alterado.",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            document = adopt_untracked_subject_file(self.config, self.database, finding)
        except (LookupError, ValueError, OSError, sqlite3.Error) as exc:
            QMessageBox.warning(self.main_window, _("Não foi possível adotar"), str(exc))
            self._refresh_reconciliation_report()
            return
        self.indexer.submit(document)
        self.main_window.inbox_page.set_import_status(
            _("{name} foi adotado sem mover o ficheiro.").format(name=document.current_path.name)
        )
        self._refresh_reconciliation_report()

    def _drop_missing_record(self, finding: ReconciliationFinding) -> None:
        if finding.reason is not FindingReason.MISSING_DOCUMENT or finding.document is None:
            return
        answer = QMessageBox.question(
            self.main_window,
            _("Remover registo em falta?"),
            _(
                "Será removido apenas o registo local e o índice de pesquisa. "
                "Nenhum ficheiro será apagado. Se o ficheiro reaparecer durante a operação, "
                "ficará visível para poder ser adotado novamente."
            ),
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            removed = drop_missing_document(self.database, finding.document)
        except sqlite3.Error as exc:
            LOGGER.exception("Could not drop missing catalog record")
            QMessageBox.warning(self.main_window, _("Não foi possível remover"), str(exc))
            self._refresh_reconciliation_report()
            return
        if not removed:
            QMessageBox.information(
                self.main_window,
                _("Registo mantido"),
                _("O ficheiro ou o registo mudou desde a verificação. Nada foi removido."),
            )
        self._refresh_reconciliation_report()

    def _dismiss_reconciliation_finding(self, finding: ReconciliationFinding) -> None:
        try:
            dismissed = dismiss_finding(self.database, finding)
        except sqlite3.Error as exc:
            QMessageBox.warning(self.main_window, _("Não foi possível guardar"), str(exc))
            return
        if dismissed:
            self.main_window.inbox_page.set_import_status(
                _("A ocorrência foi marcada como revista. O ficheiro não foi alterado.")
            )
            self._refresh_reconciliation_report()

    def _unregister_adopted_file(self, file_id: int) -> None:
        document = self.database.get_file(file_id)
        if document is None or document.origin != "adopted":
            self._refresh_reconciliation_report()
            return
        answer = QMessageBox.question(
            self.main_window,
            _("Remover do catálogo?"),
            _(
                "{name} deixará de aparecer na pesquisa e nos recentes. "
                "O ficheiro permanecerá exatamente onde está."
            ).format(name=document.current_path.name),
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            removed = unregister_adopted_document(self.database, document)
        except sqlite3.Error as exc:
            LOGGER.exception("Could not unregister adopted file")
            QMessageBox.warning(self.main_window, _("Não foi possível remover"), str(exc))
            self._refresh_reconciliation_report()
            return
        if not removed:
            QMessageBox.information(
                self.main_window,
                _("Registo mantido"),
                _("O registo mudou desde que a página foi aberta. Nada foi removido."),
            )
        else:
            self.main_window.inbox_page.set_import_status(
                _("{name} saiu do catálogo; o ficheiro ficou no lugar.").format(
                    name=document.current_path.name
                )
            )
        self._refresh_reconciliation_report()

    def _refresh_reconciliation_report(self) -> None:
        try:
            report = scan_reconciliation(self.config, self.database)
            visible_findings(self.database, report)
        except sqlite3.Error as exc:
            LOGGER.exception("Could not refresh reconciliation findings")
            QMessageBox.warning(self.main_window, _("Verificação incompleta"), str(exc))
            return
        self.main_window.inbox_page.set_reconciliation_report(report)
        self._refresh()

    def _organise_item(self, inbox_id: int) -> None:
        if self.prompt.current_item_id == inbox_id:
            self.prompt.raise_()
            self.prompt.activateWindow()
            return
        self.prompt_queue = deque(item for item in self.prompt_queue if item != inbox_id)
        self.prompt_queue.appendleft(inbox_id)
        if self.prompt.current_item_id is not None:
            self.prompt.close()
        else:
            self._show_next_prompt()

    def _show_next_prompt(self) -> None:
        if self.prompt.current_item_id is not None:
            return
        while self.prompt_queue:
            inbox_id = self.prompt_queue.popleft()
            item = self.database.get_inbox_item(inbox_id)
            if item is None or item.status not in {"pending", "error"} or not item.path.is_file():
                continue
            subjects = self.database.list_subjects()
            if not subjects:
                self.show_main("disciplinas")
                return
            guess = self._filing_guess(item.original_name, subjects)
            self.database.update_inbox_suggestion(item.id, guess.subject_id, guess.kind)
            refreshed = self.database.get_inbox_item(item.id)
            self.prompt.show_item(
                refreshed or item, subjects, guess, name_template=self.config.filename_template
            )
            return

    def _file_item(
        self,
        inbox_id: int,
        subject_id: int,
        kind: str,
        filename: str,
        create_task: bool,
        due_date: date | None,
    ) -> None:
        try:
            document = self.filer.file_document(inbox_id, subject_id, kind, filename)
        except FilingError as exc:
            self.prompt_queue.appendleft(inbox_id)
            self._show_next_prompt()
            self.prompt.show_error(str(exc))
            self._refresh()
            return
        if create_task:
            self.database.add_task(
                f"Rever {Path(filename).stem}", subject_id, due_date, document.id
            )
        self.indexer.submit(document)
        subject = self.database.get_subject(subject_id)
        self.tray.notify(
            _("Ficheiro organizado"),
            _("{name} foi guardado em {destination}.").format(
                name=document.current_path.name,
                destination=f"{subject.name if subject else kind} / {kind}",
            ),
        )
        self._refresh()
        QTimer.singleShot(120, self._show_next_prompt)

    def _return_item(self, inbox_id: int) -> None:
        watcher = self.watcher
        observing = watcher is not None and watcher.running
        was_paused = watcher.paused if observing and watcher is not None else False
        destination: Path | None = None
        ignore_seconds = 30.0
        if observing and watcher is not None and not was_paused:
            watcher.set_paused(True)
        try:
            destination = self.filer.return_to_downloads(inbox_id)
        except FilingError as exc:
            pending_return = None
            with suppress(sqlite3.Error):
                pending_return = next(
                    (
                        event
                        for event in self.database.list_pending_returns()
                        if event.inbox_id == inbox_id
                    ),
                    None,
                )
            if pending_return is not None:
                destination = pending_return.destination_path
                ignore_seconds = float("inf")
            item = self.database.get_inbox_item(inbox_id)
            if item is not None:
                self.prompt_queue.appendleft(inbox_id)
                self._show_next_prompt()
                self.prompt.show_error(str(exc))
            else:
                QMessageBox.warning(self.main_window, _("Não foi possível devolver"), str(exc))
            return
        finally:
            if watcher is not None and destination is not None:
                watcher.ignore_self_move(destination, seconds=ignore_seconds)
            if observing and watcher is not None and not was_paused:
                watcher.set_paused(False)
        if destination is None:  # pragma: no cover - success assigns a path
            return
        self.tray.notify(
            _("Ficheiro devolvido"),
            _("{name} voltou para Downloads.").format(name=destination.name),
        )
        self._refresh()
        QTimer.singleShot(120, self._show_next_prompt)

    def _prompt_finished(self, _inbox_id: int) -> None:
        self._refresh()
        QTimer.singleShot(120, self._show_next_prompt)

    def _undo(self) -> None:
        try:
            item = self.filer.undo_latest_filing()
        except FilingError as exc:
            QMessageBox.warning(self.main_window, _("Não foi possível desfazer"), str(exc))
            return
        if item is None:
            self.tray.notify(
                _("Nada para desfazer"),
                _("Ainda não existe uma organização reversível."),
            )
            return
        subjects = self.database.list_subjects()
        guess = self._filing_guess(item.original_name, subjects)
        self.database.update_inbox_suggestion(item.id, guess.subject_id, guess.kind)
        self.prompt_queue.appendleft(item.id)
        self.tray.notify(
            _("Organização desfeita"),
            _("{name} voltou à Caixa de Entrada.").format(name=item.path.name),
        )
        self._refresh()
        self._show_next_prompt()

    def _filing_guess(self, filename: str, subjects: list[Subject]) -> FilingGuess:
        return guess_filing(filename, subjects, self.database.filing_hints())

    def _add_subject(self) -> None:
        dialog = SubjectDialog(parent=self.main_window)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name, code, color, keywords = dialog.values
        folder = self.filer.subject_folder_name(name, code)
        try:
            subject = self.database.add_subject(name, code, color, keywords, folder)
            self.filer.ensure_subject_structure(subject)
        except (sqlite3.IntegrityError, OSError) as exc:
            QMessageBox.warning(
                self.main_window,
                _("Não foi possível criar"),
                _("Já existe uma disciplina ou pasta com esse nome.\n\n{error}").format(error=exc),
            )
            return
        if not self.config.initialized:
            self.config.initialized = True
            self.config.ensure_directories()
            self.config.save()
            self._restart_watcher()
        self._refresh()

    def _edit_subject(self, subject_id: int) -> None:
        subject = self.database.get_subject(subject_id)
        if subject is None:
            return
        dialog = SubjectDialog(subject, self.main_window)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name, code, color, keywords = dialog.values
        try:
            updated = self.database.update_subject(
                subject.id, name, code, color, keywords, subject.folder_name
            )
            self.filer.ensure_subject_structure(updated)
        except sqlite3.IntegrityError as exc:
            QMessageBox.warning(
                self.main_window,
                _("Não foi possível guardar"),
                _("Já existe uma disciplina com esse nome.\n\n{error}").format(error=exc),
            )
            return
        self._refresh()

    def _archive_subject(self, subject_id: int) -> None:
        if self.database.count_subjects() <= 1:
            QMessageBox.information(
                self.main_window,
                _("Mantém uma disciplina ativa"),
                _("Cria outra disciplina antes de arquivar esta."),
            )
            return
        subject = self.database.get_subject(subject_id)
        if subject is None:
            return
        answer = QMessageBox.question(
            self.main_window,
            _("Arquivar disciplina?"),
            _(
                "{name} deixa de aparecer nas escolhas. Os ficheiros e tarefas não são apagados."
            ).format(name=subject.name),
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.database.archive_subject(subject_id)
            self._refresh()

    def _restore_subject(self, subject_id: int) -> None:
        subject = self.database.get_subject(subject_id)
        if subject is None or subject.active:
            return
        conflicts = self.database.find_active_subject_conflicts(subject_id)
        if conflicts:
            QMessageBox.warning(
                self.main_window,
                _("Não foi possível restaurar"),
                _(
                    "Já existe uma disciplina ativa com o mesmo nome ou pasta: {names}. "
                    "Edita-a primeiro para libertar o nome."
                ).format(names=", ".join(conflicts)),
            )
            return
        answer = QMessageBox.question(
            self.main_window,
            _("Reativar disciplina?"),
            _(
                "{name} volta a aparecer nas escolhas de arquivo. "
                "Os ficheiros e tarefas não foram alterados."
            ).format(name=subject.name),
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            restored = self.database.set_subject_active(subject_id, True)
            self.filer.ensure_subject_structure(restored)
        except (sqlite3.Error, OSError) as exc:
            QMessageBox.warning(self.main_window, _("Não foi possível restaurar"), str(exc))
            return
        self._refresh()

    def _view_subject_files(self, subject_id: int) -> None:
        subject = self.database.get_subject(subject_id)
        if subject is None:
            return
        documents = self.database.list_subject_files(subject_id)
        dialog = SubjectFilesDialog(
            subject,
            documents,
            self.config.university_root / subject.folder_name,
            self.main_window,
        )
        dialog.open_requested.connect(self._open_path)
        dialog.reindex_requested.connect(self._reindex_document)
        dialog.exec()

    def _reindex_document(self, file_id: int) -> None:
        """Queue one document for extraction again."""

        document = self.database.get_file(file_id)
        if document is None:
            return
        self.indexer.reindex(document)

    def _retry_failed_indexes(self) -> None:
        """Queue every failed document for extraction again."""

        for document in self.database.list_failed_index_documents():
            self.indexer.reindex(document)
        self.main_window.search_page.refresh_index_status()

    def _organise_selection(self, inbox_ids: object) -> None:
        if not isinstance(inbox_ids, (tuple, list)):
            return
        items: list[InboxItem] = []
        failures: list[str] = []
        for inbox_id in inbox_ids:
            item = self.database.get_inbox_item(int(inbox_id))
            if item is None or item.status == "recovery":
                continue
            if not item.path.is_file():
                failures.append(f"{item.original_name}: o ficheiro já não está disponível")
                continue
            items.append(item)
        if not items and not failures:
            return
        subjects = self.database.list_subjects()
        if not subjects:
            self.show_main("disciplinas")
            return
        dialog = BulkFilingDialog(items, subjects, self.config.filename_template, self.main_window)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        subject_id, kind, create_task, due_date = dialog.values
        subject = next(entry for entry in subjects if entry.id == subject_id)
        filed = 0
        handled: list[int] = []
        for item in items:
            final_name = render_final_name(
                self.config.filename_template,
                subject_name=subject.name,
                subject_code=subject.code,
                kind=kind,
                original_name=item.original_name,
                when=item.detected_at,
            )
            try:
                document = self.filer.file_document(item.id, subject_id, kind, final_name)
            except FilingError as exc:
                failures.append(f"{item.original_name}: {exc}")
                continue
            handled.append(item.id)
            filed += 1
            if create_task:
                self.database.add_task(
                    _("Rever {name}").format(name=Path(item.original_name).stem),
                    subject_id,
                    due_date,
                    document.id,
                )
            self.indexer.submit(document)
        self.prompt_queue = deque(item for item in self.prompt_queue if item not in handled)
        if self.prompt.current_item_id in handled:
            self.prompt.close()
        if failures:
            shown = "; ".join(failures[:3])
            if len(failures) > 3:
                shown += " " + _("e mais {count}").format(count=len(failures) - 3)
            message = (
                _("{count} organizados, {failed} com erro. Revê: {errors}.")
                if filed != 1
                else _("{count} organizado, {failed} com erro. Revê: {errors}.")
            ).format(count=filed, failed=len(failures), errors=shown)
        else:
            message = (
                _("{count} organizados.").format(count=filed)
                if filed != 1
                else _("{count} organizado.").format(count=filed)
            )
        message += " " + _("Nenhum ficheiro foi substituído ou apagado. ")
        message += _("Só a organização mais recente pode ser desfeita.")
        self.main_window.inbox_page.set_import_status(message)
        self._refresh()

    def _save_settings(self, values: SettingsPayload) -> None:
        if self._manual_import_active or (
            self.watcher is not None and self.watcher.manual_import_running
        ):
            self.main_window.settings_page.set_status(
                _("Espera que a importação de Downloads termine antes de guardar."),
                error=True,
            )
            return
        previous = copy.deepcopy(self.config)
        startup_changed = False
        try:
            self.config.university_root = values["university_root"]
            self.config.downloads_dir = values["downloads_dir"]
            self.config.allowed_extensions = parse_extensions(str(values["extensions"]))
            if not self.config.allowed_extensions:
                raise ValueError("Adiciona pelo menos uma extensão aceite.")
            self.config.minimum_file_size = int(values["minimum_file_size"])
            self.config.prompt_timeout_seconds = int(values["prompt_timeout_seconds"])
            self.config.reminder_lead_days = int(values["reminder_lead_days"])
            self.config.filename_template = str(values["filename_template"])
            self.config.theme = str(values["theme"])
            self.config.language = str(values["language"])
            self.config.check_updates_on_launch = bool(values["check_updates_on_launch"])
            self.config.watch_enabled = bool(values["watch_enabled"])
            desired_startup = bool(values["launch_at_login"])
            self.config.launch_at_login = desired_startup
            self.config.ensure_directories()
            for subject in self.database.list_subjects():
                self.filer.ensure_subject_structure(subject)
            if desired_startup != previous.launch_at_login:
                set_launch_at_login(desired_startup)
                startup_changed = True
            self.config.save()
        except (ValueError, OSError, TypeError) as exc:
            if startup_changed:
                with suppress(OSError):
                    set_launch_at_login(previous.launch_at_login)
            self._restore_config(previous)
            self.main_window.settings_page.set_status(str(exc), error=True)
            return
        self.prompt.set_timeout(self.config.prompt_timeout_seconds)
        application = QApplication.instance()
        if isinstance(application, QApplication):
            apply_theme(application, get_theme(self.config.theme))
        self._restart_watcher()
        self.main_window.settings_page.load_config(self.config)
        self.main_window.settings_page.set_status(_("Definições guardadas."))
        self._refresh()

    def _reset_manual_import_state(self) -> None:
        self._manual_import_active = False
        self._manual_imported = 0
        self._manual_import_skipped = 0
        self._manual_import_failed = 0
        self._manual_import_deferred = 0
        self._manual_import_errors.clear()
        self.main_window.inbox_page.set_import_running(False)

    def _restore_config(self, previous: AppConfig) -> None:
        self.config.university_root = previous.university_root
        self.config.downloads_dir = previous.downloads_dir
        self.config.allowed_extensions = previous.allowed_extensions
        self.config.minimum_file_size = previous.minimum_file_size
        self.config.watch_enabled = previous.watch_enabled
        self.config.launch_at_login = previous.launch_at_login
        self.config.prompt_timeout_seconds = previous.prompt_timeout_seconds
        self.config.reminder_lead_days = previous.reminder_lead_days
        self.config.filename_template = previous.filename_template
        self.config.theme = previous.theme
        self.config.language = previous.language
        self.config.check_updates_on_launch = previous.check_updates_on_launch
        self.config.initialized = previous.initialized

    def _set_paused(self, paused: bool) -> None:
        if self.watcher is not None and self.watcher.running:
            self.watcher.set_paused(paused)
        else:
            paused = False
        self.tray.set_paused(paused)
        self._refresh()

    def _refresh(self) -> None:
        watching = self.watcher is not None and self.watcher.running
        paused = self.watcher.paused if watching and self.watcher is not None else False
        self.main_window.refresh_all(watching=watching, paused=paused)
        self.tray.update_inbox_count(self.database.count_inbox_items())
        self.tray.set_paused(paused)

    def _handle_legacy_rollback_bridge(self) -> None:
        """Retain a pre-v0.6.2 rollback folder until a later healthy launch."""

        app_dir = updater.app_directory()
        if app_dir is None:
            return
        legacy = app_dir.parent / "Organizador.old"
        if not legacy.is_dir():
            return
        marker = self.config.data_dir / "updates" / "legacy-rollback-retained"
        try:
            if marker.is_file():
                marker.unlink()
                threading.Thread(
                    target=updater.cleanup_previous_version,
                    args=(app_dir,),
                    name="update-cleanup",
                    daemon=True,
                ).start()
            else:
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text("retained\n", encoding="utf-8")
                LOGGER.info("Retaining legacy rollback folder %s until a later launch", legacy)
        except OSError:
            LOGGER.exception("Could not handle the legacy rollback folder %s", legacy)

    def _update_result_version(self, result: updater.UpdateResult) -> str:
        """Return a best-effort version label for a persisted update result."""

        transaction_id = result.transaction_id
        if len(transaction_id) != 32 or not transaction_id.isalnum():
            return ""
        manifest_path = (
            updater.updates_directory(self.config.data_dir) / transaction_id / "transaction.json"
        )
        try:
            transaction = updater.read_update_transaction(manifest_path)
        except (OSError, updater.UpdaterError, ValueError):
            return ""
        return ".".join(str(part) for part in transaction.version)

    def _show_pending_update_result(self) -> None:
        """Display persisted update outcomes once, then mark them seen."""

        try:
            results = updater.scan_unseen_update_results(self.config.data_dir)
        except OSError:
            LOGGER.exception("Could not scan persisted update results")
            return
        for result in results:
            version = self._update_result_version(result)
            if result.status is updater.UpdateResultStatus.SUCCEEDED:
                title = _("Atualização instalada")
                if version:
                    message = _("Atualização {version} instalada com sucesso.").format(
                        version=version
                    )
                else:
                    message = _("Atualização instalada com sucesso.")
                icon = QSystemTrayIcon.MessageIcon.Information
                box = QMessageBox.Icon.Information
            elif result.status is updater.UpdateResultStatus.ROLLED_BACK:
                title = _("Atualização revertida")
                if version:
                    message = _(
                        "A atualização {version} falhou e a versão anterior foi restaurada."
                    ).format(version=version)
                else:
                    message = _("A atualização falhou e a versão anterior foi restaurada.")
                icon = QSystemTrayIcon.MessageIcon.Warning
                box = QMessageBox.Icon.Warning
            else:
                title = _("Atualização falhou")
                message = _("A atualização falhou e a reposição automática não foi concluída.")
                if (
                    result.status is updater.UpdateResultStatus.FAILED_AFTER_COMMIT
                    and result.rollback_dir.is_dir()
                ):
                    message += " " + _("A versão anterior foi mantida em: {path}.").format(
                        path=result.rollback_dir
                    )
                icon = QSystemTrayIcon.MessageIcon.Critical
                box = QMessageBox.Icon.Critical
            if self.tray.available:
                self.tray.notify(title, message, icon=icon)
            elif box is QMessageBox.Icon.Information:
                QMessageBox.information(self.main_window, title, message)
            elif box is QMessageBox.Icon.Warning:
                QMessageBox.warning(self.main_window, title, message)
            else:
                QMessageBox.critical(self.main_window, title, message)
            try:
                updater.mark_scanned_update_result_seen(self.config.data_dir, result.transaction_id)
            except (OSError, ValueError, updater.UpdaterError):
                LOGGER.exception("Could not mark update result %s as seen", result.transaction_id)

    def _pending_version_text(self) -> str | None:
        """Return the pending update version label, if one is stored."""

        if self._pending_update is None:
            return None
        return ".".join(str(part) for part in self._pending_update.version)

    def _begin_update_check(self, *, automatic: bool) -> None:
        """Check for a newer release in a background thread."""

        if self._update_installing or self._update_checking:
            return
        if not updater.is_frozen():
            if not automatic:
                self.tray.notify(
                    _("Sem atualizações nesta instalação"),
                    _(
                        "A app está a correr em modo de desenvolvimento; "
                        "as atualizações aplicam-se apenas à versão instalada."
                    ),
                )
            return
        self._update_checking = True
        self._update_check_generation += 1
        generation = self._update_check_generation
        self.tray.set_update_state(checking=True, version=self._pending_version_text())

        def run() -> None:
            try:
                result: UpdateCheckResult = updater.check_latest_release()
            except Exception as exc:
                LOGGER.exception("Update check failed")
                result = UpdateCheckResult(
                    updater.UpdateCheckStatus.ERROR, error=str(exc) or type(exc).__name__
                )
            self.update_check_finished.emit(result, automatic, generation)

        threading.Thread(target=run, name="update-check", daemon=True).start()

    def _on_update_check_finished(self, result: object, automatic: bool, generation: int) -> None:
        if generation != self._update_check_generation:
            LOGGER.debug("Ignoring stale update check result")
            return
        self._update_checking = False
        if not isinstance(result, UpdateCheckResult):
            LOGGER.error("Ignoring malformed update check result")
            self.tray.set_update_state(version=self._pending_version_text())
            return
        if result.status is updater.UpdateCheckStatus.ERROR:
            self.tray.set_update_state(version=self._pending_version_text())
            if automatic:
                LOGGER.warning("Automatic update check failed: %s", result.error)
                return
            self.tray.notify(
                _("Não foi possível procurar atualizações."),
                result.error or _("Não foi possível instalar a atualização."),
                icon=QSystemTrayIcon.MessageIcon.Warning,
            )
            return
        if result.status is updater.UpdateCheckStatus.NO_UPDATE:
            self._pending_update = None
            self.tray.set_update_state(version=None)
            if not automatic:
                self.tray.notify(
                    _("Sem atualizações"),
                    _("O Organizador está atualizado."),
                )
            return
        info = result.update
        if info is None:  # pragma: no cover - guarded by the result type
            self.tray.set_update_state(version=self._pending_version_text())
            return
        self._pending_update = info
        version = ".".join(str(part) for part in info.version)
        self.tray.set_update_state(version=version)
        self.tray.notify(
            _("Atualização disponível"),
            _(
                "Organizador {version} está disponível. Escolhe "
                "“Instalar atualização” no menu do tabuleiro."
            ).format(version=version),
        )

    def _install_pending_update(self) -> None:
        info = self._pending_update
        if info is None or self._update_installing or self._update_checking:
            return
        app_dir = updater.app_directory()
        if app_dir is None:
            self.tray.notify(
                _("Atualização falhou"),
                _("A atualização só se aplica à versão instalada."),
                icon=QSystemTrayIcon.MessageIcon.Warning,
            )
            return
        self._update_installing = True
        self._abort_update_install = False
        version = ".".join(str(part) for part in info.version)
        self.tray.set_update_state(installing=True, version=version)
        self.tray.notify(
            _("A instalar atualização…"),
            _("A transferir e a verificar Organizador {version}.").format(version=version),
        )

        def run() -> None:
            transaction: UpdateTransaction | None = None
            try:
                transaction = updater.create_update_transaction(
                    app_dir,
                    info.version,
                    data_dir=self.config.data_dir,
                    old_pid=os.getpid(),
                )
                if self._abort_update_install:
                    raise _UpdateInstallAborted
                zip_path = updater.download_and_verify(
                    info.zip_url, info.sha256_url, transaction.download_dir
                )
                if self._abort_update_install:
                    raise _UpdateInstallAborted
                staging = updater.extract_to_staging(zip_path, transaction.staging_dir)
                if self._abort_update_install:
                    raise _UpdateInstallAborted
                staged_version = updater.read_staged_release_version(staging)
                if staged_version is not None and staged_version != info.version:
                    raise updater.UpdaterError(
                        _("A atualização transferida não corresponde à versão {version}.").format(
                            version=version
                        )
                    )
                updater.write_update_helper(transaction)
            except _UpdateInstallAborted:
                if transaction is not None:
                    updater.abort_update_transaction(transaction)
                self.update_install_finished.emit(None)
                return
            except Exception as exc:
                LOGGER.exception("Update installation failed")
                if transaction is not None:
                    updater.abort_update_transaction(transaction)
                message = (
                    str(exc)
                    if isinstance(exc, updater.UpdaterError)
                    else _("Não foi possível instalar a atualização.")
                )
                self.update_install_finished.emit(message)
                return
            self.update_install_finished.emit(transaction)

        threading.Thread(target=run, name="update-install", daemon=True).start()

    def _on_update_install_finished(self, result: object) -> None:
        if result is None:
            # The worker aborted during shutdown; stay quiet and exit with the app.
            self._update_installing = False
            self._update_transaction = None
            return
        if isinstance(result, UpdateTransaction):
            transaction = result
            self._update_transaction = transaction
            try:
                updater.launch_update_helper(transaction, wait_ready=False)
            except Exception as exc:
                LOGGER.exception("Could not launch the update helper")
                updater.abort_update_transaction(transaction)
                self._update_installing = False
                self._update_transaction = None
                message = (
                    str(exc)
                    if isinstance(exc, updater.UpdaterError)
                    else _("Não foi possível instalar a atualização.")
                )
                self.tray.set_update_state(version=self._pending_version_text())
                self.tray.notify(
                    _("Atualização falhou"),
                    message,
                    icon=QSystemTrayIcon.MessageIcon.Warning,
                )
                return
            self._update_restart_armed = True
            self.tray.set_update_state(
                installing=True,
                version=".".join(str(part) for part in transaction.version),
            )
            deadline = time.monotonic() + transaction.ready_timeout_seconds
            self._wait_for_helper_ready(transaction, deadline)
            return
        self._update_installing = False
        message = (
            str(result)
            if isinstance(result, str) and result
            else _("Não foi possível instalar a atualização.")
        )
        self.tray.set_update_state(version=self._pending_version_text())
        self.tray.notify(
            _("Atualização falhou"),
            message,
            icon=QSystemTrayIcon.MessageIcon.Warning,
        )

    def _wait_for_helper_ready(self, transaction: UpdateTransaction, deadline: float) -> None:
        """Restart once the helper supervises, or abort the handoff on timeout."""

        if updater.helper_ready_received(transaction):
            self.tray.notify(
                _("Atualização pronta"),
                _("A reiniciar para aplicar a atualização…"),
            )
            self.shutdown()
            return
        if time.monotonic() >= deadline:
            LOGGER.error("Update helper did not become ready; aborting the handoff")
            updater.abort_update_transaction(transaction)
            self._update_installing = False
            self._update_restart_armed = False
            self._update_transaction = None
            self.tray.set_update_state(version=self._pending_version_text())
            self.tray.notify(
                _("Atualização falhou"),
                _("Não foi possível iniciar o assistente de atualização."),
                icon=QSystemTrayIcon.MessageIcon.Warning,
            )
            return
        QTimer.singleShot(150, lambda: self._wait_for_helper_ready(transaction, deadline))

    def run_update_handshake(
        self,
        transaction: UpdateTransaction,
        recovery_bundle: RecoveryBundle | None,
        coordinator: RecoveryCoordinator,
        state: StartupState,
        *,
        background: bool,
        commit_timeout_seconds: float = 60.0,
    ) -> None:
        """Drive the ready/commit/healthy protocol from the running event loop."""

        try:
            updater.validate_update_target(
                transaction.manifest_path,
                transaction.token,
                data_dir=self.config.data_dir,
            )
        except updater.UpdaterError as exc:
            LOGGER.error("Update target validation failed: %s", exc)
            QMessageBox.critical(
                self.main_window,
                _("Atualização inválida"),
                _("A atualização não corresponde a esta instalação. Nenhum ficheiro foi alterado."),
            )
            QApplication.exit(1)
            return
        updater.mark_update_ready(transaction.manifest_path, transaction.token)
        self._poll_update_commit(
            transaction,
            recovery_bundle,
            coordinator,
            state,
            background=background,
            deadline=time.monotonic() + commit_timeout_seconds,
        )

    def _poll_update_commit(
        self,
        transaction: UpdateTransaction,
        recovery_bundle: RecoveryBundle | None,
        coordinator: RecoveryCoordinator,
        state: StartupState,
        *,
        background: bool,
        deadline: float,
    ) -> None:
        """Activate after the helper commits, or continue alone when it never does."""

        if updater.update_commit_received(transaction) or time.monotonic() >= deadline:
            if not updater.update_commit_received(transaction):
                LOGGER.warning("Update commit marker never arrived; continuing startup alone")
            self._commit_update_handshake(
                transaction, recovery_bundle, coordinator, state, background=background
            )
            return
        QTimer.singleShot(
            100,
            lambda: self._poll_update_commit(
                transaction,
                recovery_bundle,
                coordinator,
                state,
                background=background,
                deadline=deadline,
            ),
        )

    def _commit_update_handshake(
        self,
        transaction: UpdateTransaction,
        recovery_bundle: RecoveryBundle | None,
        coordinator: RecoveryCoordinator,
        state: StartupState,
        *,
        background: bool,
    ) -> None:
        """Activate services, then close data rollback and acknowledge health."""

        try:
            self.activate(state, background=background)
        except Exception:
            LOGGER.exception("Updated application failed to activate after commit")
            if recovery_bundle is not None:
                with suppress(Exception):
                    coordinator.restore_pending()
            QMessageBox.critical(
                self.main_window,
                _("Não foi possível concluir a atualização"),
                _(
                    "A nova versão não conseguiu arrancar. "
                    "Foi tentada a reposição da cópia de segurança."
                ),
            )
            QApplication.exit(1)
            return
        if recovery_bundle is not None:
            try:
                coordinator.mark_healthy(recovery_bundle)
            except Exception:
                LOGGER.exception("Could not mark the migrated data healthy")
                QMessageBox.critical(
                    self.main_window,
                    _("Não foi possível concluir a atualização"),
                    _(
                        "Os dados migrados não puderam ser validados. "
                        "A versão anterior foi mantida para recuperação manual."
                    ),
                )
                QApplication.exit(1)
                return
        with suppress(Exception):
            updater.mark_update_healthy(transaction.manifest_path, transaction.token)

    def _check_deadlines(self) -> None:
        today = date.today()
        for task in self.database.list_tasks(include_completed=False):
            if task.due_date is None or task.last_notified_on == today:
                continue
            lead = (
                task.reminder_lead_days
                if task.reminder_lead_days is not None
                else self.config.reminder_lead_days
            )
            delta = (task.due_date - today).days
            if delta > lead:
                continue
            subject = task.subject_name or _("Tarefa geral")
            self.tray.notify(
                subject,
                _("{title} {when}.").format(title=task.title, when=_deadline_copy(delta)),
            )
            self.database.mark_task_notified(task.id, today)

    def _index_finished(self, file_id: int, error: str) -> None:
        if error:
            LOGGER.warning("Indexing failed for file %s: %s", file_id, error)
        self.indexer.submit_pending()
        if self.main_window.search_page.search_edit.text().strip():
            self.main_window.search_page.search()

    def _hidden_to_tray(self) -> None:
        if self.hide_notice_shown:
            return
        self.hide_notice_shown = True
        self.tray.notify(
            _("Organizador continua ativo"),
            _("A janela fechou, mas Downloads continua a ser vigiado no tabuleiro do sistema."),
        )

    def _open_path(self, value: Path | str) -> None:
        path = Path(value)
        if not path.exists():
            QMessageBox.warning(
                self.main_window,
                _("Caminho não encontrado"),
                _("Não foi possível encontrar:\n{path}").format(path=path),
            )
            return
        try:
            os.startfile(path)
        except OSError as exc:
            QMessageBox.warning(self.main_window, _("Não foi possível abrir"), str(exc))

    def _reveal_path(self, value: Path | str) -> None:
        path = Path(value)
        if not path.exists():
            self._open_path(path)
            return
        try:
            subprocess.Popen(["explorer.exe", "/select,", str(path)])
        except OSError as exc:
            QMessageBox.warning(self.main_window, _("Não foi possível mostrar"), str(exc))
