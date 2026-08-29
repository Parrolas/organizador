"""The only application service permitted to move user files."""

from __future__ import annotations

import logging
from pathlib import Path

from organizador.config import AppConfig
from organizador.db import Database
from organizador.models import FILE_KINDS, FiledDocument, InboxItem, Subject
from organizador.paths import (
    is_direct_child,
    move_without_overwrite,
    sanitise_component,
    sanitise_filename,
    unique_path,
)

LOGGER = logging.getLogger(__name__)


class FilingError(RuntimeError):
    """A recoverable file-operation failure suitable for display to the user."""


class FilingService:
    """Perform collision-safe moves and keep persistence in sync."""

    def __init__(self, config: AppConfig, database: Database) -> None:
        self.config = config
        self.database = database

    def ingest(self, source: Path) -> InboxItem | None:
        """Move one completed eligible download into the university inbox."""

        if not self.config.accepts(source) or not is_direct_child(
            source, self.config.downloads_dir
        ):
            return None
        try:
            size = source.stat().st_size
        except OSError as exc:
            raise FilingError(f"Já não foi possível encontrar {source.name}.") from exc
        if size < self.config.minimum_file_size:
            return None

        self.config.inbox_dir.mkdir(parents=True, exist_ok=True)
        destination = unique_path(self.config.inbox_dir, source.name)
        move_without_overwrite(source, destination)
        try:
            return self.database.add_inbox_item(destination, source, source.name, size)
        except Exception as exc:
            rollback = unique_path(source.parent, source.name)
            try:
                move_without_overwrite(destination, rollback)
            except OSError:
                LOGGER.exception("Failed to roll back an inbox move")
            raise FilingError(
                f"{source.name} foi devolvido porque não foi possível registar o movimento."
            ) from exc

    def file_document(
        self,
        inbox_id: int,
        subject_id: int,
        kind: str,
        requested_name: str,
    ) -> FiledDocument:
        """Move an inbox document to a subject/type folder."""

        item = self.database.get_inbox_item(inbox_id)
        subject = self.database.get_subject(subject_id)
        if item is None:
            raise FilingError("Este ficheiro já não está na Caixa de Entrada.")
        if subject is None or not subject.active:
            raise FilingError("Escolhe uma disciplina ativa.")
        if kind not in FILE_KINDS:
            raise FilingError("Escolhe um tipo de documento válido.")
        if not item.path.is_file():
            self.database.set_inbox_status(inbox_id, "error", "Ficheiro não encontrado")
            raise FilingError(f"Não foi possível encontrar {item.original_name}.")

        filename = self._name_with_original_extension(requested_name, item.original_name)
        folder = self.config.university_root / subject.folder_name / kind
        destination = unique_path(folder, filename)
        self.database.set_inbox_status(inbox_id, "filing")
        try:
            move_without_overwrite(item.path, destination)
        except OSError as exc:
            self.database.set_inbox_status(inbox_id, "error", str(exc))
            raise FilingError(
                "O ficheiro ainda está a ser usado por outra aplicação. Fecha-o e tenta novamente."
            ) from exc

        try:
            return self.database.record_filing(inbox_id, subject_id, kind, destination)
        except Exception as exc:
            rollback = unique_path(item.path.parent, item.path.name)
            try:
                move_without_overwrite(destination, rollback)
                self.database.set_inbox_status(inbox_id, "pending")
            except OSError:
                LOGGER.exception("Failed to roll back a subject filing")
            raise FilingError(
                "O movimento foi revertido porque não foi possível atualizar o histórico."
            ) from exc

    def return_to_downloads(self, inbox_id: int) -> Path:
        """Return non-university material to Downloads without overwriting."""

        item = self.database.get_inbox_item(inbox_id)
        if item is None or not item.path.is_file():
            raise FilingError("O ficheiro já não está disponível para devolver.")
        destination = unique_path(self.config.downloads_dir, item.original_name)
        try:
            move_without_overwrite(item.path, destination)
            self.database.record_return(inbox_id, destination)
        except OSError as exc:
            raise FilingError(
                "Não foi possível devolver o ficheiro. Fecha-o noutras aplicações e tenta de novo."
            ) from exc
        except Exception as exc:
            rollback = unique_path(item.path.parent, item.path.name)
            try:
                move_without_overwrite(destination, rollback)
            except OSError:
                LOGGER.exception("Failed to roll back a return to Downloads")
            raise FilingError("Não foi possível registar a devolução do ficheiro.") from exc
        return destination

    def undo_latest_filing(self) -> InboxItem | None:
        """Restore the latest filed document to the university inbox."""

        event = self.database.latest_undoable_filing()
        if event is None:
            return None
        if not event.destination_path.is_file():
            raise FilingError(
                "O último ficheiro organizado já não está no destino. O histórico não foi alterado."
            )
        restored_path = unique_path(self.config.inbox_dir, event.source_path.name)
        try:
            move_without_overwrite(event.destination_path, restored_path)
            self.database.mark_filing_undone(event, restored_path)
        except OSError as exc:
            raise FilingError(
                "Não foi possível desfazer porque o ficheiro está a ser usado."
            ) from exc
        except Exception as exc:
            rollback = unique_path(event.destination_path.parent, event.destination_path.name)
            try:
                move_without_overwrite(restored_path, rollback)
            except OSError:
                LOGGER.exception("Failed to roll back an undo operation")
            raise FilingError("Não foi possível atualizar o histórico ao desfazer.") from exc
        if event.inbox_id is None:  # pragma: no cover - schema invariant
            return None
        return self.database.get_inbox_item(event.inbox_id)

    def ensure_subject_structure(self, subject: Subject) -> None:
        """Create each configured type folder for a subject."""

        subject_root = self.config.university_root / subject.folder_name
        for kind in FILE_KINDS:
            (subject_root / kind).mkdir(parents=True, exist_ok=True)

    @staticmethod
    def subject_folder_name(name: str, code: str = "") -> str:
        """Build a stable readable folder name for a new subject."""

        prefix = f"{code.strip()} - " if code.strip() else ""
        return sanitise_component(f"{prefix}{name.strip()}", fallback="Disciplina")

    @staticmethod
    def _name_with_original_extension(requested: str, original: str) -> str:
        original_suffix = Path(original).suffix
        safe = sanitise_filename(requested or original)
        if original_suffix and Path(safe).suffix.casefold() != original_suffix.casefold():
            safe = f"{Path(safe).stem}{original_suffix}"
        return safe
