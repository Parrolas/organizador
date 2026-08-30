"""The only application service permitted to move user files."""

from __future__ import annotations

import logging
from pathlib import Path

from organizador.config import MANUAL_IMPORT_BATCH_LIMIT, AppConfig
from organizador.db import Database
from organizador.models import (
    FILE_KINDS,
    ExistingDownload,
    ExistingDownloadsPlan,
    FiledDocument,
    InboxItem,
    Subject,
)
from organizador.paths import (
    IncompleteMoveError,
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

    def plan_existing_downloads(self) -> ExistingDownloadsPlan:
        """Build a deterministic, read-only plan for a confirmed manual import."""

        try:
            paths = sorted(
                self.config.downloads_dir.iterdir(),
                key=lambda path: (path.name.casefold(), path.name),
            )
        except OSError as exc:
            raise FilingError("Não foi possível ler a pasta Downloads configurada.") from exc

        candidates: list[ExistingDownload] = []
        for path in paths:
            if not self.config.accepts(path):
                continue
            candidate = ExistingDownload.capture(path)
            if candidate is None or candidate.size < self.config.minimum_file_size:
                continue
            candidates.append(candidate)
        return ExistingDownloadsPlan(len(candidates), tuple(candidates[:MANUAL_IMPORT_BATCH_LIMIT]))

    def ingest(
        self,
        source: Path,
        *,
        expected: ExistingDownload | None = None,
    ) -> InboxItem | None:
        """Move one completed eligible download into the university inbox."""

        source = source.absolute()
        if not self.config.accepts(source) or not is_direct_child(
            source, self.config.downloads_dir
        ):
            return None
        current = ExistingDownload.capture(source)
        if current is None:
            if source.exists():
                return None
            exc = FileNotFoundError(source)
            raise FilingError(f"Já não foi possível encontrar {source.name}.") from exc
        if expected is not None and current != expected:
            return None
        size = current.size
        if size < self.config.minimum_file_size:
            return None

        try:
            self.config.inbox_dir.mkdir(parents=True, exist_ok=True)
            destination = unique_path(self.config.inbox_dir, source.name)
            if expected is not None and not expected.still_matches():
                return None
            expected_identity = (
                (expected.device, expected.inode, expected.size, expected.modified_ns)
                if expected is not None
                else None
            )
            move_without_overwrite(source, destination, expected_identity=expected_identity)
        except IncompleteMoveError as exc:
            raise FilingError(
                f"{source.name} ficou em Downloads, mas uma cópia incompleta pode ter ficado "
                f"em {exc.leftover_path}. Compara os ficheiros antes de a remover."
            ) from exc
        except OSError as exc:
            raise FilingError(
                f"{source.name} mudou ou ainda está a ser usado e ficou em Downloads."
            ) from exc
        try:
            return self.database.add_inbox_item(destination, source, source.name, size)
        except Exception as exc:
            rollback = unique_path(source.parent, source.name)
            try:
                move_without_overwrite(destination, rollback)
            except OSError as rollback_error:
                LOGGER.exception("Failed to roll back an inbox move")
                raise FilingError(
                    f"Não foi possível registar {source.name}. O ficheiro ficou em {destination}."
                ) from rollback_error
            raise FilingError(
                f"Não foi possível registar {source.name}; foi devolvido a Downloads "
                f"como {rollback.name}."
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
        try:
            pending = self.database.begin_document_filing(inbox_id, subject_id, kind, destination)
        except Exception as exc:
            raise FilingError("Não foi possível preparar o histórico da organização.") from exc
        try:
            move_without_overwrite(item.path, destination)
        except IncompleteMoveError as exc:
            raise FilingError(
                "A organização ficou incompleta. O original e a cópia foram mantidos; "
                "revê ambos na Caixa de Entrada antes de continuar."
            ) from exc
        except OSError as exc:
            try:
                self.database.cancel_pending_inbox_operation(
                    pending.id, status="error", error=str(exc)
                )
            except Exception:
                LOGGER.exception("Failed to cancel a prepared filing")
            raise FilingError(
                "O ficheiro ainda está a ser usado por outra aplicação. Fecha-o e tenta novamente."
            ) from exc

        try:
            return self.database.record_filing(
                inbox_id,
                subject_id,
                kind,
                destination,
                pending_event_id=pending.id,
            )
        except Exception as exc:
            rollback = unique_path(item.path.parent, item.path.name)
            rolled_back = False
            try:
                move_without_overwrite(destination, rollback)
            except OSError:
                LOGGER.exception("Failed to roll back a subject filing")
            else:
                try:
                    self.database.cancel_pending_inbox_operation(pending.id, current_path=rollback)
                    rolled_back = True
                except Exception:
                    LOGGER.exception("Failed to cancel the rolled-back filing")
            message = (
                "O movimento foi revertido porque não foi possível atualizar o histórico."
                if rolled_back
                else "Não foi possível atualizar o histórico. "
                "Revê a Caixa de Entrada antes de repetir."
            )
            raise FilingError(message) from exc

    def return_to_downloads(self, inbox_id: int) -> Path:
        """Return non-university material to Downloads without overwriting."""

        item = self.database.get_inbox_item(inbox_id)
        if item is None or not item.path.is_file():
            raise FilingError("O ficheiro já não está disponível para devolver.")
        destination = unique_path(self.config.downloads_dir, item.original_name)
        try:
            pending = self.database.begin_return(inbox_id, destination)
        except Exception as exc:
            raise FilingError("Não foi possível preparar o histórico da devolução.") from exc
        try:
            move_without_overwrite(item.path, destination)
        except IncompleteMoveError as exc:
            raise FilingError(
                "A devolução ficou incompleta. O original e a cópia foram mantidos; "
                "revê a Caixa de Entrada e Downloads antes de continuar."
            ) from exc
        except OSError as exc:
            try:
                self.database.cancel_pending_inbox_operation(
                    pending.id, status="error", error=str(exc)
                )
            except Exception:
                LOGGER.exception("Failed to cancel a prepared return")
            raise FilingError(
                "Não foi possível devolver o ficheiro. Fecha-o noutras aplicações e tenta de novo."
            ) from exc
        try:
            self.database.record_return(inbox_id, destination, pending_event_id=pending.id)
        except Exception as exc:
            rollback = unique_path(item.path.parent, item.path.name)
            try:
                move_without_overwrite(destination, rollback)
            except OSError:
                LOGGER.exception("Failed to roll back a return to Downloads")
            else:
                try:
                    self.database.cancel_pending_inbox_operation(pending.id, current_path=rollback)
                except Exception:
                    LOGGER.exception("Failed to cancel the rolled-back return")
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
            pending = self.database.begin_filing_undo(event, restored_path)
        except Exception as exc:
            raise FilingError("Não foi possível preparar o histórico para desfazer.") from exc
        try:
            move_without_overwrite(event.destination_path, restored_path)
        except IncompleteMoveError as exc:
            raise FilingError(
                "A operação de desfazer ficou incompleta. O original e a cópia foram mantidos; "
                "revê ambos na Caixa de Entrada antes de continuar."
            ) from exc
        except OSError as exc:
            try:
                self.database.cancel_pending_undo(pending.id)
            except Exception:
                LOGGER.exception("Failed to cancel a prepared undo")
            raise FilingError(
                "Não foi possível desfazer porque o ficheiro está a ser usado."
            ) from exc
        try:
            self.database.mark_filing_undone(event, restored_path)
        except Exception as exc:
            rollback = unique_path(event.destination_path.parent, event.destination_path.name)
            try:
                move_without_overwrite(restored_path, rollback)
            except OSError:
                LOGGER.exception("Failed to roll back an undo operation")
            else:
                try:
                    self.database.cancel_pending_undo(pending.id)
                except Exception:
                    LOGGER.exception("Failed to cancel the rolled-back undo")
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
