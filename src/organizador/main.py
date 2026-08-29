"""Executable entry point and single-instance Windows coordination."""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMessageBox

from organizador.config import APP_NAME, AppConfig, default_data_dir
from organizador.controller import AppController
from organizador.ui.icons import app_icon
from organizador.ui.theme import apply_theme


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
    return parser


def main(argv: list[str] | None = None) -> int:
    """Create Qt, enforce one instance and run the application."""

    arguments = build_parser().parse_args(argv)
    application = QApplication(sys.argv[:1])
    application.setApplicationName(APP_NAME)
    application.setOrganizationName(APP_NAME)
    application.setQuitOnLastWindowClosed(False)
    application.setWindowIcon(app_icon())
    apply_theme(application)

    try:
        config = AppConfig.load(arguments.data_dir)
    except ValueError as exc:
        config = AppConfig(data_dir=arguments.data_dir or default_data_dir())
        QMessageBox.warning(
            None,
            "Definições danificadas",
            "Não foi possível ler as definições guardadas. "
            f"A app abriu com valores seguros para poderes corrigi-las.\n\n{exc}",
        )
    instance = SingleInstance(config.data_dir)
    if not arguments.smoke_test and not instance.acquire():
        return 0

    controller = AppController(config)
    instance.show_requested.connect(controller.show_main)
    controller.start(background=arguments.background, smoke_test=arguments.smoke_test)
    if arguments.smoke_test:
        QTimer.singleShot(900, controller.shutdown)
    exit_code = application.exec()
    return int(exit_code)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
