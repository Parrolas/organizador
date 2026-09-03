"""The only application service permitted to move user files."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from organizador.config import (
    DEFAULT_FILENAME_TEMPLATE,
    MANUAL_IMPORT_BATCH_LIMIT,
    AppConfig,
)
from organizador.db import METRIC_COLLISIONS_RENAMED, Database
from organizador.i18n import _
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
    resolve_contained,
    sanitise_component,
    sanitise_filename,
    unique_path,
)

LOGGER = logging.getLogger(__name__)


def _restore_original_extension(requested: str, original: str) -> str:
    """Keep the requested stem but force the original file extension."""

    original_suffix = Path(original).suffix
    safe = sanitise_filename(requested or original)
    if original_suffix and Path(safe).suffix.casefold() != original_suffix.casefold():
        safe = f"{Path(safe).stem}{original_suffix}"
    return safe


def render_name_template(
    template: str,
    *,
    subject_name: str,
    subject_code: str,
    kind: str,
    original_name: str,
    when: datetime,
) -> str:
    """Expand a user filename template; unknown text is kept, never blanked."""

    replacements = {
        "{disciplina}": subject_name.strip(),
        "{codigo}": subject_code.strip(),
        "{tipo}": kind,
        "{nome_original}": original_name,
        "{data}": f"{when.year:04d}-{when.month:02d}-{when.day:02d}",
        "{ano}": f"{when.year:04d}",
        "{mes}": f"{when.month:02d}",
        "{dia}": f"{when.day:02d}",
    }
    result = template
    for token, value in replacements.items():
        result = result.replace(token, value)
    return result


def render_final_name(
    template: str = DEFAULT_FILENAME_TEMPLATE,
    *,
    subject_name: str,
    subject_code: str,
    kind: str,
    original_name: str,
    when: datetime,
) -> str:
    """Render a template and restore the original extension, exactly as filing will."""

    rendered = render_name_template(
        template,
        subject_name=subject_name,
        subject_code=subject_code,
        kind=kind,
        original_name=original_name,
        when=when,
    )
    return _restore_original_extension(rendered, original_name)


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
            raise FilingError(_("Não foi possível ler a pasta Downloads configurada.")) from exc

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
            raise FilingError(
                _("Já não foi possível encontrar {name}.").format(name=source.name)
            ) from exc
        if expected is not None and current != expected:
            return None
        size = current.size
        if size < self.config.minimum_file_size:
            return None

        try:
            self.config.inbox_dir.mkdir(parents=True, exist_ok=True)
            destination, collided = self._plan_contained_destination(
                self.config.inbox_dir, source.name, self.config.inbox_dir
            )
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
                _(
                    "{name} ficou em Downloads, mas uma cópia incompleta pode ter ficado "
                    "em {leftover}. Compara os ficheiros antes de a remover."
                ).format(name=source.name, leftover=exc.leftover_path)
            ) from exc
        except OSError as exc:
            raise FilingError(
                _("{name} mudou ou ainda está a ser usado e ficou em Downloads.").format(
                    name=source.name
                )
            ) from exc
        try:
            item = self.database.add_inbox_item(destination, source, source.name, size)
        except Exception as exc:
            rollback = unique_path(source.parent, source.name)
            try:
                move_without_overwrite(destination, rollback)
            except OSError as rollback_error:
                LOGGER.exception("Failed to roll back an inbox move")
                leftover = _("Não foi possível registar {name}. O ficheiro ficou em {destination}.")
                raise FilingError(
                    leftover.format(name=source.name, destination=destination)
                ) from rollback_error
            returned = _(
                "Não foi possível registar {name}; foi devolvido a Downloads como {returned}."
            )
            raise FilingError(returned.format(name=source.name, returned=rollback.name)) from exc
        self._register_collision(collided)
        return item

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
            raise FilingError(_("Este ficheiro já não está na Caixa de Entrada."))
        if subject is None or not subject.active:
            raise FilingError(_("Escolhe uma disciplina ativa."))
        if kind not in FILE_KINDS:
            raise FilingError(_("Escolhe um tipo de documento válido."))
        if not item.path.is_file():
            self.database.set_inbox_status(inbox_id, "error", "Ficheiro não encontrado")
            raise FilingError(
                _("Não foi possível encontrar {name}.").format(name=item.original_name)
            )

        filename = self._name_with_original_extension(requested_name, item.original_name)
        folder = self.config.university_root / subject.folder_name / kind
        destination, collided = self._plan_contained_destination(
            folder, filename, self.config.university_root
        )
        try:
            pending = self.database.begin_document_filing(inbox_id, subject_id, kind, destination)
        except Exception as exc:
            raise FilingError(_("Não foi possível preparar o histórico da organização.")) from exc
        try:
            move_without_overwrite(item.path, destination)
        except IncompleteMoveError as exc:
            raise FilingError(
                _(
                    "A organização ficou incompleta. O original e a cópia foram mantidos; "
                    "revê ambos na Caixa de Entrada antes de continuar."
                )
            ) from exc
        except OSError as exc:
            try:
                self.database.cancel_pending_inbox_operation(
                    pending.id, status="error", error=str(exc)
                )
            except Exception:
                LOGGER.exception("Failed to cancel a prepared filing")
            raise FilingError(
                _(
                    "O ficheiro ainda está a ser usado por outra aplicação. "
                    "Fecha-o e tenta novamente."
                )
            ) from exc

        try:
            document = self.database.record_filing(
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
                _("O movimento foi revertido porque não foi possível atualizar o histórico.")
                if rolled_back
                else _(
                    "Não foi possível atualizar o histórico. "
                    "Revê a Caixa de Entrada antes de repetir."
                )
            )
            raise FilingError(message) from exc
        self._register_collision(collided)
        return document

    def return_to_downloads(self, inbox_id: int) -> Path:
        """Return non-university material to Downloads without overwriting."""

        item = self.database.get_inbox_item(inbox_id)
        if item is None or not item.path.is_file():
            raise FilingError(_("O ficheiro já não está disponível para devolver."))
        destination, collided = self._plan_contained_destination(
            self.config.downloads_dir, item.original_name, self.config.downloads_dir
        )
        try:
            pending = self.database.begin_return(inbox_id, destination)
        except Exception as exc:
            raise FilingError(_("Não foi possível preparar o histórico da devolução.")) from exc
        try:
            move_without_overwrite(item.path, destination)
        except IncompleteMoveError as exc:
            raise FilingError(
                _(
                    "A devolução ficou incompleta. O original e a cópia foram mantidos; "
                    "revê a Caixa de Entrada e Downloads antes de continuar."
                )
            ) from exc
        except OSError as exc:
            try:
                self.database.cancel_pending_inbox_operation(
                    pending.id, status="error", error=str(exc)
                )
            except Exception:
                LOGGER.exception("Failed to cancel a prepared return")
            raise FilingError(
                _(
                    "Não foi possível devolver o ficheiro. "
                    "Fecha-o noutras aplicações e tenta de novo."
                )
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
            raise FilingError(_("Não foi possível registar a devolução do ficheiro.")) from exc
        self._register_collision(collided)
        return destination

    def undo_latest_filing(self) -> InboxItem | None:
        """Restore the latest filed document to the university inbox."""

        event = self.database.latest_undoable_filing()
        if event is None:
            return None
        if not event.destination_path.is_file():
            raise FilingError(
                _(
                    "O último ficheiro organizado já não está no destino. "
                    "O histórico não foi alterado."
                )
            )
        restored_path, collided = self._plan_contained_destination(
            self.config.inbox_dir, event.source_path.name, self.config.inbox_dir
        )
        try:
            pending = self.database.begin_filing_undo(event, restored_path)
        except Exception as exc:
            raise FilingError(_("Não foi possível preparar o histórico para desfazer.")) from exc
        try:
            move_without_overwrite(event.destination_path, restored_path)
        except IncompleteMoveError as exc:
            raise FilingError(
                _(
                    "A operação de desfazer ficou incompleta. O original e a cópia foram mantidos; "
                    "revê ambos na Caixa de Entrada antes de continuar."
                )
            ) from exc
        except OSError as exc:
            try:
                self.database.cancel_pending_undo(pending.id)
            except Exception:
                LOGGER.exception("Failed to cancel a prepared undo")
            raise FilingError(
                _("Não foi possível desfazer porque o ficheiro está a ser usado.")
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
            raise FilingError(_("Não foi possível atualizar o histórico ao desfazer.")) from exc
        self._register_collision(collided)
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
        return _restore_original_extension(requested, original)

    def _plan_destination(self, directory: Path, filename: str) -> tuple[Path, bool]:
        """Plan a collision-safe destination and report whether a rename was needed."""

        destination = unique_path(directory, filename)
        return destination, destination.name != sanitise_filename(filename)

    def _plan_contained_destination(
        self, directory: Path, filename: str, root: Path
    ) -> tuple[Path, bool]:
        """Plan a destination that cannot escape its managed root via reparse points."""

        destination, collided = self._plan_destination(directory, filename)
        try:
            resolve_contained(destination, root)
        except OSError as exc:
            raise FilingError(
                _("A pasta de destino não é segura: {path}.").format(path=directory)
            ) from exc
        return destination, collided

    def _register_collision(self, collided: bool) -> None:
        """Count one safely renamed collision for the activity summary."""

        if collided:
            self.database.increment_metric(METRIC_COLLISIONS_RENAMED)
