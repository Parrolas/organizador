"""Executable entry point and single-instance Windows coordination."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import logging
import sys
from contextlib import suppress
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMessageBox

from organizador import updater
from organizador.config import APP_NAME, AppConfig, default_data_dir
from organizador.controller import AppController
from organizador.db import Database, DatabaseHealthError, NewerDatabaseError
from organizador.i18n import _, set_language
from organizador.logging_setup import configure_logging, log_uncaught_exception
from organizador.recovery import RecoveryBundle, RecoveryCoordinator, RecoveryError
from organizador.ui.icons import app_icon
from organizador.ui.theme import apply_theme, get_theme

LOGGER = logging.getLogger(__name__)


class SingleInstance(QObject):
    """Notify the running process instead of starting a second file watcher."""

    show_requested = Signal()

    def __init__(self, data_dir: Path) -> None:
        super().__init__()
        digest = hashlib.sha1(str(data_dir.resolve()).encode("utf-8")).hexdigest()[:12]
        self.name = f"organizador-{digest}"
        self.server = QLocalServer(self)
        self.server.newConnection.connect(self._receive)

    def acquire(self) -> bool:
        """Listen for future launches or ask an existing process to show itself."""

        probe = QLocalSocket()
        probe.connectToServer(self.name)
        if probe.waitForConnected(250):
            probe.write(b"show")
            probe.waitForBytesWritten(250)
            probe.disconnectFromServer()
            return False
        QLocalServer.removeServer(self.name)
        return self.server.listen(self.name)

    def _receive(self) -> None:
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            if socket is None:
                continue
            socket.waitForReadyRead(100)
            socket.readAll()
            socket.disconnectFromServer()
            self.show_requested.emit()


def build_parser() -> argparse.ArgumentParser:
    """Create the small command-line surface used by startup and tests."""

    parser = argparse.ArgumentParser(description="Organizador de ficheiros de estudo")
    parser.add_argument(
        "--background", action="store_true", help="Arrancar apenas no tabuleiro do sistema"
    )
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--data-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--update-manifest", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--update-token", help=argparse.SUPPRESS)
    return parser


def split_update_arguments(manifest: Path | None, token: str | None) -> tuple[Path, str] | None:
    """Return paired update handshake arguments, or ``None`` for normal startup."""

    if manifest is None and token is None:
        return None
    if manifest is None or token is None:
        raise ValueError("--update-manifest and --update-token must be used together")
    return manifest, token


def load_config_safely(data_dir: Path) -> tuple[AppConfig, Exception | None]:
    """Return safe defaults when any settings-loading failure reaches startup."""

    try:
        return AppConfig.load(data_dir), None
    except Exception as exc:
        LOGGER.exception("Could not load application settings")
        return AppConfig(data_dir=data_dir), exc


def _set_app_user_model_id() -> None:
    """Give the packaged app a stable identity for tray notifications."""

    if not getattr(sys, "frozen", False):
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            ctypes.c_wchar_p("Parrolas.Organizador")
        )
    except Exception:  # pragma: no cover - cosmetic Windows integration
        LOGGER.warning("Could not set the application user model id", exc_info=True)


def main(argv: list[str] | None = None) -> int:
    """Create Qt, enforce one instance and run the application."""

    arguments = build_parser().parse_args(argv)
    target_data_dir = arguments.data_dir or default_data_dir()
    sys.excepthook = log_uncaught_exception
    _set_app_user_model_id()
    try:
        configure_logging(target_data_dir)
    except Exception as exc:
        logging_error: Exception | None = exc
    else:
        logging_error = None
    application = QApplication(sys.argv[:1])
    application.setApplicationName(APP_NAME)
    application.setOrganizationName(APP_NAME)
    application.setQuitOnLastWindowClosed(False)
    application.setWindowIcon(app_icon())

    if logging_error is not None:
        QMessageBox.critical(
            None,
            _("Não foi possível iniciar o registo"),
            _(
                "A pasta de dados da aplicação não está disponível. "
                "Nenhum ficheiro foi alterado.\n\n{error}"
            ).format(error=logging_error),
        )
        return 1

    config, config_error = load_config_safely(target_data_dir)
    if config_error is not None:
        QMessageBox.warning(
            None,
            _("Definições danificadas"),
            _(
                "Não foi possível ler as definições guardadas. "
                "A app abriu com valores seguros para poderes corrigi-las.\n\n{error}"
            ).format(error=config_error),
        )
    set_language(config.language)
    apply_theme(application, get_theme(config.theme))

    try:
        update_arguments = split_update_arguments(arguments.update_manifest, arguments.update_token)
    except ValueError:
        QMessageBox.critical(
            None,
            _("Atualização inválida"),
            _("Os argumentos da atualização estão incompletos. Nenhum ficheiro foi alterado."),
        )
        return 1

    # One installation, one updater: frozen processes rendezvous on the install
    # folder so two profiles can never update the same binaries concurrently.
    install_dir = updater.app_directory()
    instance = SingleInstance(install_dir if install_dir is not None else target_data_dir)
    if not arguments.smoke_test and not instance.acquire():
        return 0

    coordinator = RecoveryCoordinator(target_data_dir)
    try:
        restored = coordinator.restore_pending()
    except Exception as exc:
        LOGGER.exception("Could not recover a pending migration")
        QMessageBox.critical(
            None,
            _("Não foi possível recuperar os dados"),
            _(
                "Existe uma cópia de segurança de migração que não pôde ser restaurada "
                "automaticamente. Nenhum ficheiro foi alterado.\n\n{error}"
            ).format(error=exc),
        )
        return 1
    if restored is not None:
        LOGGER.warning(
            "Restored pre-migration backup from an interrupted update: %s", restored.path
        )

    with suppress(Exception):
        updater.prune_abandoned_update_state(target_data_dir)

    database = Database(config.database_path)
    bundle: RecoveryBundle | None = None
    try:
        if database.inspect_schema().requires_migration:
            bundle = coordinator.prepare_migration()
        database.initialize()
        database.validate_health().require_healthy()
    except NewerDatabaseError:
        LOGGER.error("Refusing to open a database from a newer application version")
        QMessageBox.critical(
            None,
            _("Versão da base de dados mais recente"),
            _(
                "Esta base de dados foi criada por uma versão mais recente do Organizador. "
                "Abre a versão mais recente da app. Nenhum ficheiro foi alterado."
            ),
        )
        return 1
    except Exception as exc:
        LOGGER.exception("Could not migrate application data")
        with suppress(Exception):
            coordinator.restore_pending()
        QMessageBox.critical(
            None,
            _("Não foi possível atualizar os dados"),
            _(
                "A aplicação não conseguiu preparar o catálogo local. "
                "Foi tentada a reposição da cópia de segurança.\n\n{error}"
            ).format(error=exc),
        )
        return 1

    try:
        controller = AppController(config, database=database)
    except Exception as exc:
        LOGGER.exception("Could not initialize application data")
        if bundle is not None:
            with suppress(Exception):
                coordinator.restore_pending()
        QMessageBox.critical(
            None,
            _("Não foi possível abrir os dados"),
            _(
                "A aplicação não conseguiu abrir o catálogo local. "
                "Consulta organizador.log antes de tentar novamente.\n\n{error}"
            ).format(error=exc),
        )
        return 1
    instance.show_requested.connect(controller.show_main)

    if update_arguments is not None:
        manifest_path = update_arguments[0]
        try:
            transaction = updater.read_update_transaction(manifest_path)
        except updater.UpdaterError as exc:
            LOGGER.error("Could not read the update transaction: %s", exc)
            QMessageBox.critical(
                None,
                _("Atualização inválida"),
                _("A atualização não pôde ser validada. Nenhum ficheiro foi alterado."),
            )
            return 1
        if transaction.result_path.exists():
            LOGGER.info(
                "Update %s already has a recorded outcome; starting normally",
                transaction.transaction_id,
            )
        elif not updater.is_frozen():
            QMessageBox.critical(
                None,
                _("Atualização inválida"),
                _("A atualização só se aplica à versão instalada."),
            )
            return 1
        else:
            state = controller.prepare()
            QTimer.singleShot(
                0,
                lambda: controller.run_update_handshake(
                    transaction,
                    bundle,
                    coordinator,
                    state,
                    background=arguments.background,
                ),
            )
            return int(application.exec())

    try:
        controller.start(background=arguments.background, smoke_test=arguments.smoke_test)
    except Exception as exc:
        LOGGER.exception("Application failed to start after migration")
        if bundle is not None:
            with suppress(Exception):
                coordinator.restore_pending()
        QMessageBox.critical(
            None,
            _("Não foi possível concluir o arranque"),
            _(
                "A aplicação não conseguiu concluir o arranque. "
                "Foi tentada a reposição da cópia de segurança.\n\n{error}"
            ).format(error=exc),
        )
        return 1
    if bundle is not None:
        try:
            coordinator.mark_healthy(bundle)
        except (RecoveryError, DatabaseHealthError, OSError) as exc:
            LOGGER.exception("Could not mark migrated data healthy")
            with suppress(Exception):
                coordinator.restore_pending()
            QMessageBox.critical(
                None,
                _("Não foi possível atualizar os dados"),
                _(
                    "Os dados migrados não puderam ser validados. "
                    "Foi tentada a reposição da cópia de segurança.\n\n{error}"
                ).format(error=exc),
            )
            return 1
    with suppress(Exception):
        coordinator.prune_healthy_backups()
    if arguments.smoke_test:
        QTimer.singleShot(900, controller.shutdown)
    exit_code = application.exec()
    return int(exit_code)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
