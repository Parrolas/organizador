"""Main application pages for the Operate-mode desktop surface."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import TypedDict, cast

from PySide6.QtCore import QDate, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from organizador import __version__
from organizador.config import AppConfig
from organizador.db import Database
from organizador.models import (
    FiledDocument,
    FindingReason,
    InboxItem,
    ReconciliationFinding,
    ReconciliationReport,
    StudyTask,
    Subject,
)
from organizador.reconcile import DISMISSIBLE_FINDING_REASONS, visible_findings
from organizador.ui.theme import DANGER_SOFT, MUTED, WARNING_SOFT
from organizador.ui.widgets import (
    EmptyState,
    PageHeading,
    PathActionRow,
    button,
    clear_layout,
    format_day,
    format_size,
    label,
)


class SettingsPayload(TypedDict):
    """Validated value types emitted by the settings page."""

    university_root: Path
    downloads_dir: Path
    extensions: str
    minimum_file_size: int
    prompt_timeout_seconds: int
    watch_enabled: bool
    launch_at_login: bool


def _page_layout(widget: QWidget) -> QVBoxLayout:
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(34, 28, 34, 30)
    layout.setSpacing(22)
    return layout


def _scroll_list() -> tuple[QScrollArea, QWidget, QVBoxLayout]:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    content = QWidget()
    layout = QVBoxLayout(content)
    layout.setContentsMargins(0, 0, 4, 0)
    layout.setSpacing(9)
    area.setWidget(content)
    return area, content, layout


class HomePage(QWidget):
    """At-a-glance inbox and next-deadline view."""

    open_path = Signal(object)
    open_university = Signal()
    show_inbox = Signal()

    def __init__(self, database: Database, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.database = database
        layout = _page_layout(self)
        open_button = button("Abrir pasta Universidade")
        open_button.clicked.connect(self.open_university)
        layout.addWidget(
            PageHeading(
                "O teu semestre, arrumado.",
                "Os downloads novos aparecem aqui antes de irem para uma disciplina.",
                [open_button],
            )
        )

        self.intake_strip = QFrame()
        self.intake_strip.setObjectName("IntakeStrip")
        intake_layout = QHBoxLayout(self.intake_strip)
        intake_layout.setContentsMargins(18, 13, 14, 13)
        self.watch_label = label("A preparar a vigilância de Downloads…", "RowTitle")
        self.inbox_button = button("Caixa de Entrada", variant="quiet")
        self.inbox_button.clicked.connect(self.show_inbox)
        intake_layout.addWidget(self.watch_label, 1)
        intake_layout.addWidget(self.inbox_button)
        layout.addWidget(self.intake_strip)

        columns = QGridLayout()
        columns.setHorizontalSpacing(16)
        columns.setColumnStretch(0, 3)
        columns.setColumnStretch(1, 2)

        recent_panel = QFrame()
        recent_panel_layout = QVBoxLayout(recent_panel)
        recent_panel_layout.setContentsMargins(0, 7, 8, 0)
        recent_panel_layout.setSpacing(11)
        recent_panel_layout.addWidget(label("Organizados recentemente", "SectionTitle"))
        self.recent_layout = QVBoxLayout()
        self.recent_layout.setSpacing(8)
        recent_panel_layout.addLayout(self.recent_layout)
        recent_panel_layout.addStretch(1)
        columns.addWidget(recent_panel, 0, 0)

        task_panel = QFrame()
        task_panel_layout = QVBoxLayout(task_panel)
        task_panel_layout.setContentsMargins(8, 7, 0, 0)
        task_panel_layout.setSpacing(11)
        task_panel_layout.addWidget(label("Próximos prazos", "SectionTitle"))
        self.deadline_layout = QVBoxLayout()
        self.deadline_layout.setSpacing(8)
        task_panel_layout.addLayout(self.deadline_layout)
        task_panel_layout.addStretch(1)
        columns.addWidget(task_panel, 0, 1)

        activity_panel = QFrame()
        activity_panel.setObjectName("Panel")
        activity_layout = QVBoxLayout(activity_panel)
        activity_layout.setContentsMargins(18, 14, 18, 16)
        activity_layout.setSpacing(8)
        activity_layout.addWidget(label("Tranquilidade", "SectionTitle"))
        self.activity_label = label("", "Muted")
        self.activity_label.setWordWrap(True)
        activity_layout.addWidget(self.activity_label)
        columns.addWidget(activity_panel, 1, 0, 1, 2)
        columns.setRowStretch(0, 1)
        layout.addLayout(columns, 1)

    def refresh(self, *, watching: bool, paused: bool) -> None:
        """Refresh the operational summary."""

        inbox_count = self.database.count_inbox_items()
        if paused:
            self.watch_label.setText("Vigilância em pausa. Os novos downloads ficam onde estão.")
            self.intake_strip.setObjectName("WarningStrip")
        elif watching:
            self.watch_label.setText(
                "Downloads vigiado. Só os formatos de estudo configurados entram."
            )
            self.intake_strip.setObjectName("IntakeStrip")
        else:
            self.watch_label.setText("A vigilância de Downloads está desligada nas Definições.")
            self.intake_strip.setObjectName("WarningStrip")
        self.intake_strip.style().unpolish(self.intake_strip)
        self.intake_strip.style().polish(self.intake_strip)
        self.inbox_button.setText(
            f"Caixa de Entrada ({inbox_count})" if inbox_count else "Caixa de Entrada"
        )

        clear_layout(self.recent_layout)
        recent = self.database.list_recent_files(limit=7)
        if not recent:
            empty_copy = label(
                "O primeiro documento organizado aparece aqui. "
                "A app não toca nos ficheiros antigos sem pedires.",
                "Muted",
            )
            empty_copy.setWordWrap(True)
            self.recent_layout.addWidget(empty_copy)
        else:
            for document in recent:
                recent_subject = self.database.get_subject(document.subject_id)
                detail = (
                    f"{recent_subject.name if recent_subject else 'Disciplina'}  ·  "
                    f"{document.kind}  ·  {format_size(document.size)}"
                )
                self.recent_layout.addWidget(
                    PathActionRow(
                        document.current_path.name,
                        detail,
                        document.current_path,
                        lambda path: self.open_path.emit(path),
                    )
                )

        clear_layout(self.deadline_layout)
        tasks = self.database.list_tasks(include_completed=False)[:6]
        if not tasks:
            empty_copy = label(
                "Ainda não há prazos. Cria uma tarefa ou associa-a quando organizares um ficheiro.",
                "Muted",
            )
            empty_copy.setWordWrap(True)
            self.deadline_layout.addWidget(empty_copy)
        else:
            for task in tasks:
                row = QFrame()
                row.setObjectName("ListRow")
                row_layout = QVBoxLayout(row)
                row_layout.setContentsMargins(13, 10, 13, 10)
                row_layout.setSpacing(2)
                title = label(task.title, "RowTitle")
                title.setWordWrap(True)
                row_layout.addWidget(title)
                task_subject = task.subject_name or "Geral"
                row_layout.addWidget(
                    label(f"{task_subject}  ·  {format_day(task.due_date)}", "Muted")
                )
                self.deadline_layout.addWidget(row)
        self._refresh_activity()

    def _refresh_activity(self) -> None:
        """Show the lifetime safety record as simple, honest counts."""

        summary = self.database.activity_summary()
        parts: list[str] = []
        if summary.organized:
            parts.append(
                f"{summary.organized} ficheiro{'s' if summary.organized != 1 else ''} "
                f"organizado{'s' if summary.organized != 1 else ''}"
            )
        if summary.collisions_renamed:
            count = summary.collisions_renamed
            parts.append(
                f"{count} colisã{'o' if count == 1 else 'ões'} de nomes resolvida"
                f"{'s' if count != 1 else ''} sem substituir nada"
            )
        if summary.operations_recovered:
            count = summary.operations_recovered
            parts.append(
                f"{count} operaçã{'o' if count == 1 else 'ões'} interrompida"
                f"{'s' if count != 1 else ''} recuperada{'s' if count != 1 else ''}"
            )
        if summary.adopted:
            parts.append(
                f"{summary.adopted} ficheiro{'s' if summary.adopted != 1 else ''} "
                f"adotado{'s' if summary.adopted != 1 else ''} sem mover"
            )
        if summary.undone:
            count = summary.undone
            parts.append(
                f"{count} organizaçã{'o' if count == 1 else 'ões'} "
                f"desfeita{'s' if count != 1 else ''}"
            )
        if summary.returned:
            count = summary.returned
            parts.append(f"{count} devoluçã{'o' if count == 1 else 'ões'} a Downloads")
        if parts:
            self.activity_label.setText(" · ".join(parts) + ".")
        else:
            self.activity_label.setText(
                "A app ainda não tem histórico. Organiza o primeiro ficheiro para começar."
            )


class InboxPage(QWidget):
    """Review every file that still needs a destination."""

    organise_requested = Signal(int)
    return_requested = Signal(int)
    open_path = Signal(object)
    import_existing_requested = Signal()
    adopt_requested = Signal(object)
    drop_record_requested = Signal(object)
    dismiss_finding_requested = Signal(object)
    unregister_requested = Signal(int)

    def __init__(
        self,
        database: Database,
        config: AppConfig,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.database = database
        self.config = config
        self.reconciliation_report: ReconciliationReport | None = None
        layout = _page_layout(self)
        self.import_button = button("Importar de Downloads…")
        self.import_button.clicked.connect(self.import_existing_requested.emit)
        layout.addWidget(
            PageHeading(
                "Caixa de Entrada",
                "Nada é arquivado sem uma decisão. Organiza agora ou deixa para mais tarde.",
                [self.import_button],
            )
        )
        self.summary_label = label("", "PageSubtitle")
        layout.addWidget(self.summary_label)
        self.import_status_label = label("", "Muted")
        self.import_status_label.setWordWrap(True)
        self.import_status_label.hide()
        layout.addWidget(self.import_status_label)
        area, _, self.items_layout = _scroll_list()
        layout.addWidget(area, 1)

    def set_import_running(self, running: bool) -> None:
        """Prevent overlapping confirmed batches while the worker checks files."""

        self.import_button.setEnabled(not running)
        self.import_button.setText("A importar…" if running else "Importar de Downloads…")

    def set_import_status(self, message: str) -> None:
        """Show non-modal progress or aggregate batch feedback."""

        self.import_status_label.setText(message)
        self.import_status_label.setVisible(bool(message))

    def set_reconciliation_report(self, report: ReconciliationReport) -> None:
        """Retain unresolved startup findings for safe, path-specific review."""

        self.reconciliation_report = report

    def refresh(self) -> None:
        """Rebuild the pending-file list."""

        items = self.database.list_inbox_items()
        adopted_documents = self.database.list_adopted_files()
        recovery_count = sum(item.status == "recovery" for item in items)
        pending_count = len(items) - recovery_count
        manual_findings = self._manual_findings()
        summary_parts: list[str] = []
        if pending_count and recovery_count:
            summary_parts.append(
                f"{pending_count} ficheiro{'s' if pending_count != 1 else ''} por decidir · "
                f"{recovery_count} precisa{'m' if recovery_count != 1 else ''} de recuperação"
            )
        elif recovery_count:
            summary_parts.append(
                f"{recovery_count} ficheiro{'s' if recovery_count != 1 else ''} "
                f"precisa{'m' if recovery_count != 1 else ''} de recuperação manual"
            )
        elif pending_count:
            summary_parts.append(
                f"{pending_count} ficheiro{'s' if pending_count != 1 else ''} por decidir"
            )
        if manual_findings:
            count = len(manual_findings)
            summary_parts.append(
                f"{count} ocorrência{'s' if count != 1 else ''} do histórico "
                f"precisa{'m' if count != 1 else ''} de revisão"
            )
        report = self.reconciliation_report
        if report is not None and (report.incomplete or report.truncated):
            summary_parts.append("verificação incompleta")
        summary = " · ".join(summary_parts) if summary_parts else "A caixa está vazia"
        self.summary_label.setText(summary)
        clear_layout(self.items_layout)
        if not items and not manual_findings:
            empty = EmptyState(
                "Tudo no lugar",
                "Quando terminares um download elegível, ele aparece aqui e num pequeno popup.",
            )
            self.items_layout.addWidget(empty)
        subjects = {subject.id: subject for subject in self.database.list_subjects()}
        for item in items:
            self.items_layout.addWidget(self._row(item, subjects))
        if manual_findings:
            self.items_layout.addWidget(label("Revisão manual do histórico", "SectionTitle"))
            for finding in manual_findings:
                self.items_layout.addWidget(self._finding_row(finding))
        if adopted_documents:
            self.items_layout.addWidget(label("Ficheiros adotados", "SectionTitle"))
            for document in adopted_documents:
                self.items_layout.addWidget(self._adopted_row(document, subjects))
        self.items_layout.addStretch(1)

    def _row(self, item: InboxItem, subjects: dict[int, Subject]) -> QFrame:
        row = QFrame()
        row.setObjectName("ListRow")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(17, 13, 13, 13)
        row_layout.setSpacing(14)

        copy = QVBoxLayout()
        copy.setSpacing(3)
        title = label(item.original_name, "RowTitle")
        title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        copy.addWidget(title)
        if item.status == "recovery":
            metadata = (
                f"Recuperação manual necessária  ·  {format_size(item.size)}  ·  "
                f"{item.detected_at.strftime('%d/%m às %H:%M')}"
            )
        else:
            subject = subjects.get(item.suggested_subject_id or -1)
            suggestion = subject.name if subject else "Sem sugestão"
            metadata = (
                f"{format_size(item.size)}  ·  {item.detected_at.strftime('%d/%m às %H:%M')}  ·  "
                f"Sugestão: {suggestion} / {item.suggested_kind}"
            )
        copy.addWidget(label(metadata, "Muted"))
        if item.last_error:
            error = label(item.last_error, "ErrorText")
            error.setWordWrap(True)
            copy.addWidget(error)
        row_layout.addLayout(copy, 1)

        open_button = button(
            "Abrir Universidade" if item.status == "recovery" else "Abrir",
            variant="quiet",
        )
        open_path = self.config.university_root if item.status == "recovery" else item.path
        open_button.clicked.connect(lambda: self.open_path.emit(open_path))
        row_layout.addWidget(open_button)
        if item.status == "recovery":
            downloads_button = button("Abrir Downloads")
            downloads_button.clicked.connect(lambda: self.open_path.emit(self.config.downloads_dir))
            row_layout.addWidget(downloads_button)
            return row
        return_button = button("Não é da universidade")
        return_button.setToolTip("Devolver este ficheiro a Downloads")
        return_button.clicked.connect(lambda: self.return_requested.emit(item.id))
        organise = button("Organizar", variant="primary")
        organise.clicked.connect(lambda: self.organise_requested.emit(item.id))
        row_layout.addWidget(return_button)
        row_layout.addWidget(organise)
        return row

    def _manual_findings(self) -> tuple[ReconciliationFinding, ...]:
        report = self.reconciliation_report
        if report is None:
            return ()
        return visible_findings(self.database, report)

    def _finding_row(self, finding: ReconciliationFinding) -> QFrame:
        path = finding.path
        details = {
            FindingReason.UNTRACKED_SUBJECT_FILE: (
                "Encontrado numa disciplina sem registo. Não foi movido nem alterado."
            ),
            FindingReason.MISSING_DOCUMENT: (
                "Documento registado que já não está no destino esperado."
            ),
            FindingReason.BROKEN_UNDO_EVENT: (
                "Organização que não pode ser desfeita enquanto o ficheiro estiver em falta."
            ),
            FindingReason.PENDING_FILING_SOURCE: (
                "Origem de uma organização interrompida; não foi alterada no arranque."
            ),
            FindingReason.PENDING_FILING_DESTINATION: (
                "Destino de uma organização interrompida; compara antes de continuar."
            ),
            FindingReason.PENDING_RETURN_SOURCE: (
                "Origem de uma devolução interrompida; não foi alterada no arranque."
            ),
            FindingReason.PENDING_RETURN_DESTINATION: (
                "Destino em Downloads de uma devolução interrompida; compara os ficheiros."
            ),
            FindingReason.PENDING_UNDO_SOURCE: (
                "Origem de uma operação de desfazer interrompida; não foi alterada no arranque."
            ),
            FindingReason.PENDING_UNDO_DESTINATION: (
                "Operação de desfazer interrompida; confirma as pastas antes de continuar."
            ),
            FindingReason.LEGACY_INTERRUPTED_UNDO: (
                "Ficheiro restaurado por uma operação de desfazer interrompida."
            ),
            FindingReason.UNSAFE_PATH: (
                "O caminho registado já não é um ficheiro normal. Não foi seguido nem alterado."
            ),
        }
        row = QFrame()
        row.setObjectName("ListRow")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(17, 13, 13, 13)
        copy = QVBoxLayout()
        copy.setSpacing(3)
        copy.addWidget(label(path.name, "RowTitle"))
        detail_label = label(details[finding.reason], "ErrorText")
        detail_label.setWordWrap(True)
        copy.addWidget(detail_label)
        path_label = label(str(path), "Muted")
        path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        copy.addWidget(path_label)
        row_layout.addLayout(copy, 1)
        expected_parent = path.parent
        target = expected_parent if expected_parent.is_dir() else self.config.university_root
        open_button = button("Abrir pasta", variant="quiet")
        open_button.clicked.connect(lambda: self.open_path.emit(target))
        row_layout.addWidget(open_button)
        if finding.reason is FindingReason.UNTRACKED_SUBJECT_FILE:
            adopt_button = button("Adotar", variant="primary")
            adopt_button.setToolTip("Adicionar à pesquisa sem mover o ficheiro")
            adopt_button.clicked.connect(lambda: self.adopt_requested.emit(finding))
            row_layout.addWidget(adopt_button)
        elif finding.reason is FindingReason.MISSING_DOCUMENT:
            drop_button = button("Remover registo")
            drop_button.setToolTip("Remover apenas o registo local; nenhum ficheiro é apagado")
            drop_button.clicked.connect(lambda: self.drop_record_requested.emit(finding))
            row_layout.addWidget(drop_button)
        if finding.reason in DISMISSIBLE_FINDING_REASONS:
            dismiss_button = button("Marcar revisto", variant="quiet")
            dismiss_button.clicked.connect(lambda: self.dismiss_finding_requested.emit(finding))
            row_layout.addWidget(dismiss_button)
        return row

    def _adopted_row(
        self,
        document: FiledDocument,
        subjects: dict[int, Subject],
    ) -> QFrame:
        row = QFrame()
        row.setObjectName("ListRow")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(17, 13, 13, 13)
        copy = QVBoxLayout()
        copy.setSpacing(3)
        copy.addWidget(label(document.current_path.name, "RowTitle"))
        subject = subjects.get(document.subject_id)
        copy.addWidget(
            label(
                f"{subject.name if subject else 'Disciplina'} · {document.kind} · "
                "adotado sem mover",
                "Muted",
            )
        )
        row_layout.addLayout(copy, 1)
        open_button = button("Abrir", variant="quiet")
        open_button.clicked.connect(lambda: self.open_path.emit(document.current_path))
        row_layout.addWidget(open_button)
        unregister_button = button("Remover do catálogo")
        unregister_button.clicked.connect(lambda: self.unregister_requested.emit(document.id))
        row_layout.addWidget(unregister_button)
        return row


class SearchPage(QWidget):
    """Full-text search over locally indexed study documents."""

    open_path = Signal(object)
    reveal_path = Signal(object)

    def __init__(self, database: Database, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.database = database
        layout = _page_layout(self)
        layout.addWidget(
            PageHeading(
                "Pesquisar nos apontamentos",
                "Procura palavras dentro de PDFs, documentos Office, ficheiros de texto "
                "e notebooks já organizados.",
            )
        )
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(
            "Ex.: regra da cadeia, normalização, Revolução Francesa…"
        )
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setAccessibleName("Pesquisa nos documentos")
        layout.addWidget(self.search_edit)
        self.status_label = label(
            "A pesquisa é local. PDFs digitalizados só como imagem ainda não têm "
            "texto pesquisável.",
            "PageSubtitle",
        )
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        area, _, self.results_layout = _scroll_list()
        layout.addWidget(area, 1)

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(260)
        self.timer.timeout.connect(self.search)
        self.search_edit.textChanged.connect(lambda: self.timer.start())
        self.search_edit.returnPressed.connect(self.search)

    def focus_search(self) -> None:
        """Put the cursor in the search field."""

        self.search_edit.setFocus()
        self.search_edit.selectAll()

    def search(self) -> None:
        """Execute and render a safe FTS query."""

        text = self.search_edit.text().strip()
        clear_layout(self.results_layout)
        if not text:
            self.status_label.setText(
                "A pesquisa é local. Escreve duas ou mais letras para começar."
            )
            self.results_layout.addStretch(1)
            return
        results = self.database.search(text)
        if not results:
            self.status_label.setText(
                "Sem resultados. O documento pode ainda estar a ser indexado "
                "ou ser um PDF digitalizado."
            )
            empty = EmptyState(
                "Não encontrei essa expressão",
                "Experimenta menos palavras ou confirma se o ficheiro aparece "
                "nos organizados recentes.",
            )
            self.results_layout.addWidget(empty)
            self.results_layout.addStretch(1)
            return
        self.status_label.setText(
            f"{len(results)} resultado{'s' if len(results) != 1 else ''} · "
            "os parênteses retos mostram a correspondência"
        )
        for result in results:
            row = QFrame()
            row.setObjectName("ListRow")
            row_layout = QVBoxLayout(row)
            row_layout.setContentsMargins(17, 13, 15, 13)
            row_layout.setSpacing(7)
            top = QHBoxLayout()
            copy = QVBoxLayout()
            copy.setSpacing(2)
            copy.addWidget(label(result.title, "RowTitle"))
            copy.addWidget(
                label(
                    f"{result.subject_name}  ·  {result.kind}  ·  "
                    f"{self._location_copy(result.path, result.page)}",
                    "Muted",
                )
            )
            top.addLayout(copy, 1)
            reveal = button("Mostrar na pasta", variant="quiet")
            reveal.clicked.connect(
                lambda _checked=False, path=result.path: self.reveal_path.emit(path)
            )
            open_button = button("Abrir")
            open_button.clicked.connect(
                lambda _checked=False, path=result.path: self.open_path.emit(path)
            )
            top.addWidget(reveal)
            top.addWidget(open_button)
            row_layout.addLayout(top)
            snippet = QLabel(result.snippet.replace("\n", " "))
            snippet.setWordWrap(True)
            snippet.setStyleSheet(f"color: {MUTED}; line-height: 1.35;")
            snippet.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            row_layout.addWidget(snippet)
            self.results_layout.addWidget(row)
        self.results_layout.addStretch(1)

    @staticmethod
    def _location_copy(path: Path, page: int) -> str:
        suffix = path.suffix.casefold()
        if suffix == ".pptx":
            return f"diapositivo {page}"
        if suffix == ".xlsx":
            return f"folha {page}"
        if suffix == ".docx":
            return "documento"
        return f"página {page}"


class TasksPage(QWidget):
    """Subject-linked deadlines and completion state."""

    changed = Signal()

    def __init__(self, database: Database, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.database = database
        layout = _page_layout(self)
        layout.addWidget(
            PageHeading(
                "Tarefas e prazos",
                "Mantém cada entrega junto da disciplina a que pertence.",
            )
        )

        form_panel = QFrame()
        form_panel.setObjectName("Panel")
        form_layout = QHBoxLayout(form_panel)
        form_layout.setContentsMargins(16, 14, 14, 14)
        form_layout.setSpacing(10)
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("Nova tarefa, por exemplo: entregar ficha 4")
        self.title_edit.returnPressed.connect(self._add_task)
        self.subject_combo = QComboBox()
        self.subject_combo.setMinimumWidth(170)
        self.due_check = QCheckBox("Prazo")
        self.due_check.setChecked(True)
        self.due_edit = QDateEdit(QDate.currentDate().addDays(7))
        self.due_edit.setCalendarPopup(True)
        self.due_edit.setDisplayFormat("dd/MM/yyyy")
        self.due_edit.setMinimumWidth(150)
        self.due_check.toggled.connect(self.due_edit.setEnabled)
        add = button("Adicionar", variant="primary")
        add.clicked.connect(self._add_task)
        form_layout.addWidget(self.title_edit, 1)
        form_layout.addWidget(self.subject_combo)
        form_layout.addWidget(self.due_check)
        form_layout.addWidget(self.due_edit)
        form_layout.addWidget(add)
        layout.addWidget(form_panel)
        self.error_label = label("", "ErrorText")
        layout.addWidget(self.error_label)
        area, _, self.tasks_layout = _scroll_list()
        layout.addWidget(area, 1)
        self.refresh()

    def refresh(self) -> None:
        """Refresh subject choices and the task list."""

        current_subject = self.subject_combo.currentData()
        self.subject_combo.clear()
        self.subject_combo.addItem("Geral", None)
        for subject in self.database.list_subjects():
            self.subject_combo.addItem(subject.name, subject.id)
        if current_subject is not None:
            index = self.subject_combo.findData(current_subject)
            if index >= 0:
                self.subject_combo.setCurrentIndex(index)

        clear_layout(self.tasks_layout)
        tasks = self.database.list_tasks()
        if not tasks:
            self.tasks_layout.addWidget(
                EmptyState(
                    "Sem tarefas pendentes",
                    "Adiciona a próxima entrega acima ou cria-a ao organizar um documento.",
                )
            )
            self.tasks_layout.addStretch(1)
            return
        for task in tasks:
            self.tasks_layout.addWidget(self._row(task))
        self.tasks_layout.addStretch(1)

    def _row(self, task: StudyTask) -> QFrame:
        row = QFrame()
        row.setObjectName("ListRow")
        if not task.completed and task.due_date:
            if task.due_date < date.today():
                row.setStyleSheet(f"QFrame#ListRow {{ background: {DANGER_SOFT}; }}")
            elif task.due_date == date.today():
                row.setStyleSheet(f"QFrame#ListRow {{ background: {WARNING_SOFT}; }}")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(15, 11, 11, 11)
        row_layout.setSpacing(12)
        completed = QCheckBox()
        completed.setAccessibleName(f"Concluir {task.title}")
        completed.setChecked(task.completed)
        completed.toggled.connect(
            lambda checked, task_id=task.id: self._toggle_task(task_id, checked)
        )
        row_layout.addWidget(completed)
        copy = QVBoxLayout()
        copy.setSpacing(2)
        title = label(task.title, "RowTitle")
        if task.completed:
            title.setStyleSheet(f"color: {MUTED}; text-decoration: line-through;")
        copy.addWidget(title)
        subject = task.subject_name or "Geral"
        due = self._due_copy(task)
        copy.addWidget(label(f"{subject}  ·  {due}", "Muted"))
        row_layout.addLayout(copy, 1)
        delete = button("Eliminar", variant="danger")
        delete.clicked.connect(lambda: self._delete_task(task.id))
        row_layout.addWidget(delete)
        return row

    @staticmethod
    def _due_copy(task: StudyTask) -> str:
        if task.due_date is None:
            return "Sem prazo"
        if task.completed:
            return format_day(task.due_date)
        delta = (task.due_date - date.today()).days
        if delta < 0:
            return f"Atrasada · {format_day(task.due_date)}"
        if delta == 0:
            return "Prazo hoje"
        if delta == 1:
            return "Prazo amanhã"
        return f"Prazo em {delta} dias · {format_day(task.due_date)}"

    def _add_task(self) -> None:
        title = self.title_edit.text().strip()
        if not title:
            self.error_label.setText("Escreve uma tarefa antes de adicionar.")
            self.title_edit.setFocus()
            return
        due_date = (
            cast(date, self.due_edit.date().toPython()) if self.due_check.isChecked() else None
        )
        self.database.add_task(title, self.subject_combo.currentData(), due_date)
        self.title_edit.clear()
        self.error_label.clear()
        self.refresh()
        self.changed.emit()

    def _toggle_task(self, task_id: int, completed: bool) -> None:
        self.database.set_task_completed(task_id, completed)
        self.refresh()
        self.changed.emit()

    def _delete_task(self, task_id: int) -> None:
        self.database.delete_task(task_id)
        self.refresh()
        self.changed.emit()


class SubjectsPage(QWidget):
    """Subject configuration and physical-folder access."""

    add_requested = Signal()
    edit_requested = Signal(int)
    archive_requested = Signal(int)
    open_folder = Signal(object)

    def __init__(
        self, database: Database, config: AppConfig, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.database = database
        self.config = config
        layout = _page_layout(self)
        add = button("Adicionar disciplina", variant="primary")
        add.clicked.connect(self.add_requested)
        layout.addWidget(
            PageHeading(
                "Disciplinas",
                "Os códigos e palavras-chave tornam as sugestões de arquivo mais precisas.",
                [add],
            )
        )
        self.summary_label = label("", "PageSubtitle")
        layout.addWidget(self.summary_label)
        area, _, self.subjects_layout = _scroll_list()
        layout.addWidget(area, 1)
        self.refresh()

    def refresh(self) -> None:
        """Render all active subjects."""

        subjects = self.database.list_subjects()
        plural = len(subjects) != 1
        self.summary_label.setText(
            f"{len(subjects)} disciplina{'s' if plural else ''} ativa{'s' if plural else ''}"
        )
        clear_layout(self.subjects_layout)
        if not subjects:
            empty = EmptyState(
                "Começa por uma disciplina",
                "Cria uma disciplina para a app saber onde guardar o próximo download.",
                "Adicionar disciplina",
            )
            empty.action_requested.connect(self.add_requested)
            self.subjects_layout.addWidget(empty)
            self.subjects_layout.addStretch(1)
            return
        for subject in subjects:
            self.subjects_layout.addWidget(self._row(subject))
        self.subjects_layout.addStretch(1)

    def _row(self, subject: Subject) -> QFrame:
        row = QFrame()
        row.setObjectName("ListRow")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(15, 12, 12, 12)
        row_layout.setSpacing(13)
        swatch = QFrame()
        swatch.setFixedSize(13, 13)
        swatch.setStyleSheet(
            f"background: {subject.color}; border-radius: 6px; border: 1px solid {subject.color};"
        )
        row_layout.addWidget(swatch, 0, Qt.AlignmentFlag.AlignTop)
        copy = QVBoxLayout()
        copy.setSpacing(3)
        title = subject.name + (f"  ·  {subject.code}" if subject.code else "")
        copy.addWidget(label(title, "RowTitle"))
        keyword_copy = ", ".join(subject.keywords) if subject.keywords else "Sem palavras-chave"
        copy.addWidget(label(keyword_copy, "Muted"))
        subject_path = self.config.university_root / subject.folder_name
        folder_copy = label(f"Pasta: {subject.folder_name}", "Muted")
        folder_copy.setToolTip(str(subject_path))
        folder_copy.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        copy.addWidget(folder_copy)
        row_layout.addLayout(copy, 1)
        open_button = button("Abrir pasta", variant="quiet")
        open_button.clicked.connect(
            lambda: self.open_folder.emit(self.config.university_root / subject.folder_name)
        )
        edit = button("Editar")
        edit.clicked.connect(lambda: self.edit_requested.emit(subject.id))
        archive = button("Arquivar", variant="danger")
        archive.setToolTip("Oculta a disciplina sem apagar os respetivos ficheiros")
        archive.clicked.connect(lambda: self.archive_requested.emit(subject.id))
        row_layout.addWidget(open_button)
        row_layout.addWidget(edit)
        row_layout.addWidget(archive)
        return row


class SettingsPage(QWidget):
    """File-system, intake and login settings."""

    save_requested = Signal(object)

    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.config = config
        layout = _page_layout(self)
        layout.addWidget(
            PageHeading(
                "Definições",
                "Controla exatamente quais ficheiros entram e onde ficam guardados.",
            )
        )

        panel = QFrame()
        panel.setObjectName("Panel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(22, 20, 22, 22)
        panel_layout.setSpacing(18)
        panel_layout.addWidget(label("Pastas e vigilância", "SectionTitle"))
        form = QFormLayout()
        form.setVerticalSpacing(14)
        form.setHorizontalSpacing(22)

        self.root_edit = QLineEdit()
        root_row = QHBoxLayout()
        root_row.addWidget(self.root_edit, 1)
        root_choose = button("Escolher…")
        root_choose.clicked.connect(lambda: self._choose_folder(self.root_edit))
        root_row.addWidget(root_choose)
        form.addRow("Pasta Universidade", root_row)

        self.downloads_edit = QLineEdit()
        downloads_row = QHBoxLayout()
        downloads_row.addWidget(self.downloads_edit, 1)
        downloads_choose = button("Escolher…")
        downloads_choose.clicked.connect(lambda: self._choose_folder(self.downloads_edit))
        downloads_row.addWidget(downloads_choose)
        form.addRow("Pasta Downloads", downloads_row)

        self.extensions_edit = QLineEdit()
        self.extensions_edit.setPlaceholderText(".pdf, .docx, .pptx, .ipynb")
        form.addRow("Extensões aceites", self.extensions_edit)
        self.minimum_size = QSpinBox()
        self.minimum_size.setRange(0, 100 * 1024 * 1024)
        self.minimum_size.setSuffix(" bytes")
        form.addRow("Tamanho mínimo", self.minimum_size)
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(10, 300)
        self.timeout_spin.setSuffix(" s")
        form.addRow("Tempo do popup", self.timeout_spin)
        panel_layout.addLayout(form)

        self.watch_check = QCheckBox("Vigiar novos ficheiros em Downloads")
        self.startup_check = QCheckBox("Iniciar o Organizador quando entro no Windows")
        panel_layout.addWidget(self.watch_check)
        panel_layout.addWidget(self.startup_check)
        note = label(
            "Alterar a pasta Universidade afeta os próximos ficheiros; "
            "os já organizados não são movidos automaticamente.",
            "Muted",
        )
        note.setWordWrap(True)
        panel_layout.addWidget(note)
        layout.addWidget(panel)

        action_row = QHBoxLayout()
        self.status_label = label("", "SuccessText")
        self.status_label.setWordWrap(True)
        action_row.addWidget(self.status_label, 1)
        save = button("Guardar definições", variant="primary")
        save.clicked.connect(self._save)
        action_row.addWidget(save)
        layout.addLayout(action_row)
        version_label = label(
            f"Organizador v{__version__} · código MIT · "
            "componentes de terceiros com licenças próprias",
            "Muted",
        )
        version_label.setWordWrap(True)
        layout.addWidget(version_label)
        layout.addStretch(1)
        self.load_config(config)

    def load_config(self, config: AppConfig) -> None:
        """Populate controls from current persistent settings."""

        self.config = config
        self.root_edit.setText(str(config.university_root))
        self.root_edit.setCursorPosition(0)
        self.downloads_edit.setText(str(config.downloads_dir))
        self.downloads_edit.setCursorPosition(0)
        self.extensions_edit.setText(", ".join(config.allowed_extensions))
        self.minimum_size.setValue(max(0, config.minimum_file_size))
        self.timeout_spin.setValue(config.prompt_timeout_seconds)
        self.watch_check.setChecked(config.watch_enabled)
        self.startup_check.setChecked(config.launch_at_login)

    def set_status(self, message: str, *, error: bool = False) -> None:
        """Show settings persistence feedback."""

        self.status_label.setObjectName("ErrorText" if error else "SuccessText")
        self.status_label.setText(message)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def _choose_folder(self, target: QLineEdit) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Escolher pasta", target.text())
        if selected:
            target.setText(selected)

    def _save(self) -> None:
        payload: SettingsPayload = {
            "university_root": Path(self.root_edit.text().strip()).expanduser(),
            "downloads_dir": Path(self.downloads_edit.text().strip()).expanduser(),
            "extensions": self.extensions_edit.text(),
            "minimum_file_size": self.minimum_size.value(),
            "prompt_timeout_seconds": self.timeout_spin.value(),
            "watch_enabled": self.watch_check.isChecked(),
            "launch_at_login": self.startup_check.isChecked(),
        }
        self.save_requested.emit(payload)
