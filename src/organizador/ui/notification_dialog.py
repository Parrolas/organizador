"""A persistent notification's documents, including batches spanning folders."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QDialog, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget

from organizador.i18n import _
from organizador.models import FiledDocument


class NotificationFilesDialog(QDialog):
    reveal_requested = Signal(object)

    def __init__(self, documents: list[FiledDocument], parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("Ficheiros organizados"))
        self.resize(680, 440)
        layout = QVBoxLayout(self)
        area = QScrollArea()
        area.setWidgetResizable(True)
        content = QWidget()
        rows = QVBoxLayout(content)
        for document in documents:
            text = QLabel(str(document.current_path))
            text.setWordWrap(True)
            rows.addWidget(text)
            reveal = QPushButton(_("Mostrar na pasta"))
            reveal.clicked.connect(
                lambda checked=False, path=document.current_path: self.reveal_requested.emit(path)
            )
            rows.addWidget(reveal)
        rows.addStretch()
        area.setWidget(content)
        layout.addWidget(area)
        close = QPushButton(_("Fechar"))
        close.clicked.connect(self.close)
        layout.addWidget(close)
