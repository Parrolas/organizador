"""Application orchestration between background services and Qt widgets."""

from __future__ import annotations

import copy
import logging
import os
import sqlite3
import subprocess
from collections import deque
from contextlib import suppress
from datetime import date
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from organizador.classifier import guess_filing
from organizador.config import AppConfig, parse_extensions
from organizador.db import Database
from organizador.filer import FilingError, FilingService
from organizador.indexer import DocumentIndexer
from organizador.models import ExistingDownload, FilingGuess, InboxItem, Subject
from organizador.reconcile import apply as apply_reconciliation
from organizador.reconcile import scan as scan_reconciliation
from organizador.startup import set_launch_at_login
from organizador.ui.dialogs import OnboardingDialog, SubjectDialog
from organizador.ui.main_window import MainWindow
from organizador.ui.pages import SettingsPayload
from organizador.ui.prompt import FilingPrompt
from organizador.ui.tray import TrayIcon
from organizador.watcher import DownloadWatcher

LOGGER = logging.getLogger(__name__)


class AppController(QObject):
    """Own the long-running services and marshal worker events onto Qt's thread."""

    download_ready = Signal(object)
    index_completed = Signal(int, str)
    import_completed = Signal(int)

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.config = config
        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        self._configure_logging()
        self.database = Database(config.database_path)
        self.database.initialize()
        self.filer = FilingService(config, self.database)
        self.indexer = DocumentIndexer(
            self.database,
            lambda file_id, error: self.index_completed.emit(file_id, error),
        )
        self.watcher: DownloadWatcher | None = None
        self.prompt_queue: deque[int] = deque()
        self.notified_tasks: set[int] = set()
        self.hide_notice_shown = False
        self._manual_import_active = False
        self._manual_imported = 0
        self._manual_import_skipped = 0
        self._manual_import_failed = 0
        self._manual_import_deferred = 0
        self._manual_import_errors: list[str] = []

        self.main_window = MainWindow(self.database, config)
        self.prompt = FilingPrompt(config.prompt_timeout_seconds)
        self.tray = TrayIcon(self)
        self._connect_signals()

        self.reminder_timer = QTimer(self)
        self.reminder_timer.setInterval(60_000)
        self.reminder_timer.timeout.connect(self._check_deadlines)

    def start(self, *, background: bool = False, smoke_test: bool = False) -> None:
        """Run first-time setup, start services and reveal the appropriate surface."""

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

        if configured:
            try:
                self.config.ensure_directories()
            except (ValueError, OSError) as exc:
                QMessageBox.critical(
                    self.main_window,
                    "Não foi possível abrir as pastas",
                    f"Revê as Definições antes de ativar a vigilância.\n\n{exc}",
                )
            else:
                self._reconcile_startup()
                self._restart_watcher()
                self.indexer.submit_pending()
                self.reminder_timer.start()

        self._refresh()
        if background and configured and not smoke_test:
            self.main_window.hide()
        else:
            self.main_window.show_from_tray("inicio" if configured else "disciplinas")

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
        except sqlite3.Error as exc:
            LOGGER.exception("Startup reconciliation failed")
            self.tray.notify(
                "Verificação de segurança incompleta",
                f"Não foi possível rever o histórico local: {exc}",
            )
            return

        manual_paths = {
            *remaining.untracked_subject_files,
            *(document.current_path for document in remaining.missing_documents),
            *(event.destination_path for event in remaining.broken_undo_events),
            *(event.source_path for event in remaining.pending_filing_events),
            *(event.destination_path for event in remaining.pending_filing_events),
            *(event.source_path for event in remaining.pending_return_events),
            *(event.destination_path for event in remaining.pending_return_events),
            *(event.source_path for event in remaining.pending_undo_events),
            *(event.destination_path for event in remaining.pending_undo_events),
            *(item.restored_file.path for item in remaining.legacy_interrupted_undos),
            *remaining.unsafe_paths,
        }
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
                f"{outcome.change_count} registo"
                f"{'s' if outcome.change_count != 1 else ''} da Caixa de Entrada "
                f"{'foram revistos' if outcome.change_count != 1 else 'foi revisto'}"
            )
        if manual_paths:
            count = len(manual_paths)
            details.append(
                f"{count} ficheiro{'s' if count != 1 else ''} "
                f"precisa{'m' if count != 1 else ''} de revisão manual"
            )
        if remaining.truncated:
            details.append("a verificação atingiu o limite de segurança")
        if remaining.incomplete:
            details.append("alguns caminhos não puderam ser verificados")
        self.tray.notify(
            (
                "Verificação de segurança incompleta"
                if remaining.incomplete
                else "Verificação de segurança concluída"
            ),
            ". ".join(details) + ". Abre a Caixa de Entrada para rever.",
        )

    def shutdown(self) -> None:
        """Stop worker threads before terminating Qt."""

        self.reminder_timer.stop()
        if self.watcher is not None:
            self.watcher.stop()
            self.watcher = None
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
        self.tray.quit_requested.connect(self.shutdown)
        self.main_window.hidden_to_tray.connect(self._hidden_to_tray)

        self.main_window.home_page.open_path.connect(self._open_path)
        self.main_window.home_page.open_university.connect(
            lambda: self._open_path(self.config.university_root)
        )
        self.main_window.home_page.show_inbox.connect(lambda: self.show_main("inbox"))
        self.main_window.inbox_page.organise_requested.connect(self._organise_item)
        self.main_window.inbox_page.return_requested.connect(self._return_item)
        self.main_window.inbox_page.open_path.connect(self._open_path)
        self.main_window.inbox_page.import_existing_requested.connect(
            self._import_existing_downloads
        )
        self.main_window.search_page.open_path.connect(self._open_path)
        self.main_window.search_page.reveal_path.connect(self._reveal_path)
        self.main_window.tasks_page.changed.connect(self._refresh)
        self.main_window.subjects_page.add_requested.connect(self._add_subject)
        self.main_window.subjects_page.edit_requested.connect(self._edit_subject)
        self.main_window.subjects_page.archive_requested.connect(self._archive_subject)
        self.main_window.subjects_page.open_folder.connect(self._open_path)
        self.main_window.settings_page.save_requested.connect(self._save_settings)

        self.prompt.filing_requested.connect(self._file_item)
        self.prompt.later_requested.connect(self._prompt_finished)
        self.prompt.return_requested.connect(self._return_item)

    def _restart_watcher(self) -> None:
        if self.watcher is not None:
            self.watcher.stop()
            self.watcher = None
        if not (self.config.initialized and self.database.count_subjects() > 0):
            return
        candidate = DownloadWatcher(
            self.config,
            lambda download: self.download_ready.emit(download),
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
                "Vigilância de Downloads desligada",
                f"Não foi possível abrir a pasta configurada: {exc}",
            )
            return
        self.watcher = candidate

    def _ingest_download(self, candidate: Path | ExistingDownload) -> None:
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
                self.tray.notify("Não foi possível recolher o ficheiro", str(exc))
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
                "Novo material na Caixa de Entrada",
                f"{item.original_name} está pronto para organizar.",
            )
            self._refresh()
        self._show_next_prompt()

    def _import_existing_downloads(self) -> None:
        watcher = self.watcher
        if watcher is None or not watcher.active:
            QMessageBox.warning(
                self.main_window,
                "Importação indisponível",
                "Conclui a configuração da aplicação antes de importar ficheiros.",
            )
            return
        if self._manual_import_active or watcher.manual_import_running:
            QMessageBox.information(
                self.main_window,
                "Importação em curso",
                "Espera que o lote atual termine antes de iniciar outro.",
            )
            return
        try:
            plan = self.filer.plan_existing_downloads()
        except FilingError as exc:
            QMessageBox.warning(self.main_window, "Não foi possível procurar", str(exc))
            return
        if not plan.selected:
            QMessageBox.information(
                self.main_window,
                "Nada para importar",
                "Não existem ficheiros elegíveis no nível principal de Downloads.",
            )
            return

        selected_count = len(plan.selected)
        remaining = plan.total - selected_count
        remaining_copy = (
            f" Os outros {remaining} ficam em Downloads para um próximo lote." if remaining else ""
        )
        answer = QMessageBox.question(
            self.main_window,
            "Importar ficheiros existentes?",
            f"Foram encontrados {plan.total} ficheiros elegíveis. "
            f"Serão verificados no máximo {selected_count} e movidos para a Caixa de Entrada."
            f"{remaining_copy}\n\nCada ficheiro continuará a precisar da tua confirmação.",
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
            f"A verificar {selected_count} ficheiro{'s' if selected_count != 1 else ''}…"
        )
        queued = watcher.enqueue_existing(plan.selected)
        if queued == 0:
            self._manual_import_active = False
            self.main_window.inbox_page.set_import_running(False)
            self.main_window.inbox_page.set_import_status(
                "Os ficheiros mudaram ou já estavam a ser processados e ficaram em Downloads."
            )

    def _manual_import_finished(self, worker_skipped: int) -> None:
        if not self._manual_import_active:
            return
        skipped = self._manual_import_skipped + worker_skipped
        imported = self._manual_imported
        failed = self._manual_import_failed
        parts = [f"{imported} importado{'s' if imported != 1 else ''}"]
        if skipped:
            parts.append(f"{skipped} ignorado{'s' if skipped != 1 else ''}")
        if failed:
            parts.append(f"{failed} com erro")
        message = ", ".join(parts) + "."
        if self._manual_import_deferred:
            message += (
                f" {self._manual_import_deferred} não entraram neste lote e ficaram em Downloads."
            )
        if self._manual_import_errors:
            shown_errors = "; ".join(self._manual_import_errors[:3])
            if len(self._manual_import_errors) > 3:
                shown_errors += f"; e mais {len(self._manual_import_errors) - 3}"
            message += f" Revê: {shown_errors}."
        message += " Nenhum ficheiro foi substituído ou apagado."
        self._manual_import_active = False
        self.main_window.inbox_page.set_import_running(False)
        self.main_window.inbox_page.set_import_status(message)
        self._refresh()
        self.tray.notify("Importação de Downloads concluída", message)

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
            self.prompt.show_item(refreshed or item, subjects, guess)
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
            "Ficheiro organizado",
            f"{document.current_path.name} foi guardado em "
            f"{subject.name if subject else kind} / {kind}.",
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
                QMessageBox.warning(self.main_window, "Não foi possível devolver", str(exc))
            return
        finally:
            if watcher is not None and destination is not None:
                watcher.ignore_self_move(destination, seconds=ignore_seconds)
            if observing and watcher is not None and not was_paused:
                watcher.set_paused(False)
        if destination is None:  # pragma: no cover - success assigns a path
            return
        self.tray.notify("Ficheiro devolvido", f"{destination.name} voltou para Downloads.")
        self._refresh()
        QTimer.singleShot(120, self._show_next_prompt)

    def _prompt_finished(self, _inbox_id: int) -> None:
        self._refresh()
        QTimer.singleShot(120, self._show_next_prompt)

    def _undo(self) -> None:
        try:
            item = self.filer.undo_latest_filing()
        except FilingError as exc:
            QMessageBox.warning(self.main_window, "Não foi possível desfazer", str(exc))
            return
        if item is None:
            self.tray.notify("Nada para desfazer", "Ainda não existe uma organização reversível.")
            return
        subjects = self.database.list_subjects()
        guess = self._filing_guess(item.original_name, subjects)
        self.database.update_inbox_suggestion(item.id, guess.subject_id, guess.kind)
        self.prompt_queue.appendleft(item.id)
        self.tray.notify(
            "Organização desfeita",
            f"{item.path.name} voltou à Caixa de Entrada.",
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
                "Não foi possível criar",
                f"Já existe uma disciplina ou pasta com esse nome.\n\n{exc}",
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
                "Não foi possível guardar",
                f"Já existe uma disciplina com esse nome.\n\n{exc}",
            )
            return
        self._refresh()

    def _archive_subject(self, subject_id: int) -> None:
        if self.database.count_subjects() <= 1:
            QMessageBox.information(
                self.main_window,
                "Mantém uma disciplina ativa",
                "Cria outra disciplina antes de arquivar esta.",
            )
            return
        subject = self.database.get_subject(subject_id)
        if subject is None:
            return
        answer = QMessageBox.question(
            self.main_window,
            "Arquivar disciplina?",
            f"{subject.name} deixa de aparecer nas escolhas. "
            "Os ficheiros e tarefas não são apagados.",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.database.archive_subject(subject_id)
            self._refresh()

    def _save_settings(self, values: SettingsPayload) -> None:
        if self._manual_import_active:
            self.main_window.settings_page.set_status(
                "Espera que a importação de Downloads termine antes de guardar.",
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
        self._restart_watcher()
        self.main_window.settings_page.load_config(self.config)
        self.main_window.settings_page.set_status("Definições guardadas.")
        self._refresh()

    def _restore_config(self, previous: AppConfig) -> None:
        self.config.university_root = previous.university_root
        self.config.downloads_dir = previous.downloads_dir
        self.config.allowed_extensions = previous.allowed_extensions
        self.config.minimum_file_size = previous.minimum_file_size
        self.config.watch_enabled = previous.watch_enabled
        self.config.launch_at_login = previous.launch_at_login
        self.config.prompt_timeout_seconds = previous.prompt_timeout_seconds
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

    def _check_deadlines(self) -> None:
        for task in self.database.list_tasks(include_completed=False):
            if task.id in self.notified_tasks or task.due_date is None:
                continue
            if task.due_date <= date.today():
                subject = task.subject_name or "Tarefa geral"
                when = "vence hoje" if task.due_date == date.today() else "está atrasada"
                self.tray.notify(subject, f"{task.title} {when}.")
                self.notified_tasks.add(task.id)

    def _index_finished(self, file_id: int, error: str) -> None:
        if error:
            LOGGER.warning("Indexing failed for file %s: %s", file_id, error)
        if self.main_window.search_page.search_edit.text().strip():
            self.main_window.search_page.search()

    def _hidden_to_tray(self) -> None:
        if self.hide_notice_shown:
            return
        self.hide_notice_shown = True
        self.tray.notify(
            "Organizador continua ativo",
            "A janela fechou, mas Downloads continua a ser vigiado no tabuleiro do sistema.",
        )

    def _open_path(self, value: Path | str) -> None:
        path = Path(value)
        if not path.exists():
            QMessageBox.warning(
                self.main_window,
                "Caminho não encontrado",
                f"Não foi possível encontrar:\n{path}",
            )
            return
        try:
            os.startfile(path)
        except OSError as exc:
            QMessageBox.warning(self.main_window, "Não foi possível abrir", str(exc))

    def _reveal_path(self, value: Path | str) -> None:
        path = Path(value)
        if not path.exists():
            self._open_path(path)
            return
        try:
            subprocess.Popen(["explorer.exe", "/select,", str(path)])
        except OSError as exc:
            QMessageBox.warning(self.main_window, "Não foi possível mostrar", str(exc))

    def _configure_logging(self) -> None:
        handler = RotatingFileHandler(
            self.config.log_path,
            maxBytes=1_500_000,
            backupCount=2,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        if not any(isinstance(existing, RotatingFileHandler) for existing in root.handlers):
            root.addHandler(handler)
