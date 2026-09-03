"""Crash-safe backup coordination around SQLite schema migrations."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from uuid import uuid4

from organizador.db import (
    SCHEMA_VERSION,
    Database,
    DatabaseHealthError,
    NewerDatabaseError,
)

BUNDLE_FORMAT_VERSION = 1
DATABASE_BACKUP_NAME = "database.sqlite3"
SETTINGS_BACKUP_NAME = "settings.bin"
SETTINGS_ABSENT_NAME = "settings.absent"
MANIFEST_NAME = "manifest.json"
PENDING_MARKER = "pending"
HEALTHY_MARKER = "healthy"
FAILED_MARKER = "failed"
QUARANTINED_MARKER = "quarantined"
_PROTECTED_MARKERS = (PENDING_MARKER, FAILED_MARKER, QUARANTINED_MARKER)


class RecoveryError(RuntimeError):
    """A migration backup could not be trusted or restored safely."""


@dataclass(frozen=True, slots=True)
class RecoveryBundle:
    """A validated migration snapshot and its on-disk state directory."""

    path: Path
    created_at: datetime
    database_user_version: int
    settings_present: bool


@dataclass(frozen=True, slots=True)
class _ValidatedBundle:
    bundle: RecoveryBundle
    database_path: Path
    database_sha256: str
    settings_path: Path
    settings_sha256: str


def _utc_now() -> datetime:
    return datetime.now(UTC)


class RecoveryCoordinator:
    """Create, publish, restore, and retain migration recovery bundles."""

    def __init__(
        self,
        data_dir: Path,
        *,
        database_name: str = "organizador.db",
        settings_name: str = "settings.json",
    ) -> None:
        self.data_dir = data_dir
        self.database_path = data_dir / database_name
        self.settings_path = data_dir / settings_name
        self.backups_dir = data_dir / "backups"

    def prepare_migration(self) -> RecoveryBundle | None:
        """Publish a validated pending backup only when migration is required."""

        database = Database(self.database_path)
        if self.database_path.is_symlink():
            raise RecoveryError("The database path must not be a symbolic link.")
        if not self.database_path.is_file():
            return None
        database.validate_health().require_healthy()
        inspection = database.inspect_schema()
        if inspection.user_version is not None and inspection.user_version > SCHEMA_VERSION:
            raise NewerDatabaseError(
                f"A base de dados pertence a uma versão mais recente ({inspection.user_version})."
            )
        if not inspection.requires_migration:
            return None
        if self._pending_bundle_paths():
            raise RecoveryError("A pending migration backup already exists.")
        if self.settings_path.is_symlink():
            raise RecoveryError("The settings path must not be a symbolic link.")
        if self.settings_path.exists() and not self.settings_path.is_file():
            raise RecoveryError("The settings path is not a regular file.")

        self.backups_dir.mkdir(parents=True, exist_ok=True)
        created_at = _utc_now()
        bundle_path = self.backups_dir / (f"migration-{created_at:%Y%m%dT%H%M%S%fZ}-{uuid4().hex}")
        bundle_path.mkdir()
        try:
            database_backup = bundle_path / DATABASE_BACKUP_NAME
            database.backup_to(database_backup)
            settings_present = self.settings_path.is_file()
            settings_backup = bundle_path / (
                SETTINGS_BACKUP_NAME if settings_present else SETTINGS_ABSENT_NAME
            )
            settings_bytes = self.settings_path.read_bytes() if settings_present else b""
            _write_atomic(settings_backup, settings_bytes)
            manifest = {
                "format_version": BUNDLE_FORMAT_VERSION,
                "created_at": created_at.isoformat(),
                "schema": {
                    "user_version": inspection.user_version,
                    "missing_additions": list(inspection.missing_additions),
                },
                "files": {
                    "database": {
                        "path": DATABASE_BACKUP_NAME,
                        "sha256": _sha256_file(database_backup),
                    },
                    "settings": {
                        "path": settings_backup.name,
                        "sha256": _sha256_bytes(settings_bytes),
                        "present": settings_present,
                    },
                },
            }
            manifest_bytes = (
                json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8")
            _write_atomic(bundle_path / MANIFEST_NAME, manifest_bytes)
            validated = self._validate_bundle(bundle_path)
            self._publish_pending(bundle_path)
            return validated.bundle
        except BaseException:
            if bundle_path.exists() and not (bundle_path / PENDING_MARKER).exists():
                with suppress(OSError):
                    _write_atomic(bundle_path / FAILED_MARKER, b"")
            raise

    def restore_pending(self) -> RecoveryBundle | None:
        """Restore the sole migration that never reached its health point."""

        pending = self._pending_bundle_paths()
        if not pending:
            return None
        if len(pending) != 1:
            raise RecoveryError("Multiple pending migration backups require manual review.")
        bundle_path = pending[0]
        conflicting_states = (HEALTHY_MARKER, FAILED_MARKER, QUARANTINED_MARKER)
        if any(
            (bundle_path / state).exists() or (bundle_path / state).is_symlink()
            for state in conflicting_states
        ):
            raise RecoveryError("A pending migration backup has conflicting state markers.")
        try:
            validated = self._validate_bundle(bundle_path)
        except (DatabaseHealthError, OSError, RecoveryError, ValueError) as exc:
            self._transition(bundle_path, PENDING_MARKER, QUARANTINED_MARKER)
            raise RecoveryError(f"The pending recovery bundle is not trustworthy: {exc}") from exc

        self._restore_bundle(validated)
        self._transition(bundle_path, PENDING_MARKER, FAILED_MARKER)
        return validated.bundle

    def mark_healthy(self, bundle: RecoveryBundle) -> None:
        """Atomically prevent restoration after the application health point."""

        bundle_path = self._owned_bundle_path(bundle.path)
        pending = bundle_path / PENDING_MARKER
        healthy = bundle_path / HEALTHY_MARKER
        if healthy.is_file() and not (pending.exists() or pending.is_symlink()):
            return
        if not pending.is_file() or pending.is_symlink():
            raise RecoveryError("The migration backup is no longer pending.")
        try:
            self._validate_bundle(bundle_path)
        except (DatabaseHealthError, OSError, RecoveryError, ValueError) as exc:
            self._transition(bundle_path, PENDING_MARKER, QUARANTINED_MARKER)
            raise RecoveryError(f"The migration backup is not trustworthy: {exc}") from exc

        Database(self.database_path).validate_health().require_healthy()
        inspection = Database(self.database_path).inspect_schema()
        if not inspection.is_current:
            raise RecoveryError("The migrated database has not reached the current healthy schema.")
        self._transition(bundle_path, PENDING_MARKER, HEALTHY_MARKER)

    def prune_healthy_backups(self) -> tuple[Path, ...]:
        """Keep at most the newest two healthy bundles, never beyond 30 days."""

        if not self.backups_dir.is_dir():
            return ()
        candidates: list[tuple[datetime, Path]] = []
        for path in self.backups_dir.iterdir():
            if not path.is_dir() or path.is_symlink():
                continue
            if any(
                (path / marker).exists() or (path / marker).is_symlink()
                for marker in _PROTECTED_MARKERS
            ):
                continue
            healthy = path / HEALTHY_MARKER
            if not healthy.is_file() or healthy.is_symlink():
                continue
            try:
                validated = self._validate_bundle(path)
            except (DatabaseHealthError, OSError, RecoveryError, ValueError):
                continue
            candidates.append((validated.bundle.created_at, path))

        candidates.sort(key=lambda item: (item[0], item[1].name), reverse=True)
        cutoff = _utc_now() - timedelta(days=30)
        removed: list[Path] = []
        for index, (created_at, path) in enumerate(candidates):
            if index < 2 and created_at >= cutoff:
                continue
            shutil.rmtree(path)
            removed.append(path)
        return tuple(removed)

    def _publish_pending(self, bundle_path: Path) -> None:
        _write_atomic(bundle_path / PENDING_MARKER, b"")

    def _pending_bundle_paths(self) -> tuple[Path, ...]:
        if not self.backups_dir.is_dir():
            return ()
        pending: list[Path] = []
        for path in self.backups_dir.iterdir():
            marker = path / PENDING_MARKER
            if (
                path.is_dir()
                and not path.is_symlink()
                and marker.is_file()
                and not marker.is_symlink()
            ):
                pending.append(path)
        return tuple(sorted(pending))

    def _validate_bundle(self, bundle_path: Path) -> _ValidatedBundle:
        bundle_path = self._owned_bundle_path(bundle_path)
        manifest_path = bundle_path / MANIFEST_NAME
        if not manifest_path.is_file() or manifest_path.is_symlink():
            raise RecoveryError("The recovery manifest is missing or unsafe.")
        try:
            decoded: Any = json.loads(manifest_path.read_bytes())
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RecoveryError("The recovery manifest is malformed.") from exc
        if not isinstance(decoded, dict) or decoded.get("format_version") != BUNDLE_FORMAT_VERSION:
            raise RecoveryError("The recovery manifest format is unsupported.")

        created_raw = decoded.get("created_at")
        if not isinstance(created_raw, str):
            raise RecoveryError("The recovery manifest has no creation time.")
        try:
            created_at = datetime.fromisoformat(created_raw)
        except ValueError as exc:
            raise RecoveryError("The recovery manifest creation time is invalid.") from exc
        if created_at.tzinfo is None:
            raise RecoveryError("The recovery manifest creation time has no timezone.")
        created_at = created_at.astimezone(UTC)

        schema = decoded.get("schema")
        if not isinstance(schema, dict) or type(schema.get("user_version")) is not int:
            raise RecoveryError("The recovery manifest schema version is invalid.")
        user_version = int(schema["user_version"])
        files = decoded.get("files")
        if not isinstance(files, dict):
            raise RecoveryError("The recovery manifest file list is invalid.")
        database_entry = files.get("database")
        settings_entry = files.get("settings")
        if not isinstance(database_entry, dict) or not isinstance(settings_entry, dict):
            raise RecoveryError("The recovery manifest file entries are invalid.")

        database_path, database_sha256 = self._manifest_file(
            bundle_path, database_entry, expected_name=DATABASE_BACKUP_NAME
        )
        settings_present = settings_entry.get("present")
        if not isinstance(settings_present, bool):
            raise RecoveryError("The settings presence marker is invalid.")
        expected_settings_name = SETTINGS_BACKUP_NAME if settings_present else SETTINGS_ABSENT_NAME
        settings_path, settings_sha256 = self._manifest_file(
            bundle_path, settings_entry, expected_name=expected_settings_name
        )
        if not settings_present and settings_path.stat().st_size != 0:
            raise RecoveryError("The settings absence marker is not empty.")
        database_sidecars = (Path(f"{database_path}-wal"), Path(f"{database_path}-shm"))
        if any(path.exists() or path.is_symlink() for path in database_sidecars):
            raise RecoveryError("The database backup is not standalone.")
        Database(database_path).validate_health().require_healthy()
        return _ValidatedBundle(
            bundle=RecoveryBundle(
                path=bundle_path,
                created_at=created_at,
                database_user_version=user_version,
                settings_present=settings_present,
            ),
            database_path=database_path,
            database_sha256=database_sha256,
            settings_path=settings_path,
            settings_sha256=settings_sha256,
        )

    def _manifest_file(
        self,
        bundle_path: Path,
        entry: dict[Any, Any],
        *,
        expected_name: str,
    ) -> tuple[Path, str]:
        relative = entry.get("path")
        digest = entry.get("sha256")
        if not isinstance(relative, str) or not isinstance(digest, str):
            raise RecoveryError("A recovery manifest file entry is malformed.")
        if relative != expected_name:
            raise RecoveryError("A recovery manifest path is unexpected.")
        candidate = _safe_relative_path(bundle_path, relative)
        if not candidate.is_file() or candidate.is_symlink():
            raise RecoveryError("A recovery bundle file is missing or unsafe.")
        actual = _sha256_file(candidate)
        if len(digest) != 64 or not hmac.compare_digest(actual, digest.casefold()):
            raise RecoveryError(f"Hash mismatch for {relative}.")
        return candidate, digest.casefold()

    def _restore_bundle(self, validated: _ValidatedBundle) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        database_stage = self.data_dir / (f".{self.database_path.name}.restore-{uuid4().hex}.tmp")
        settings_stage = self.data_dir / (f".{self.settings_path.name}.restore-{uuid4().hex}.tmp")
        moved_sidecars: list[tuple[Path, Path]] = []
        try:
            _copy_durable(validated.database_path, database_stage)
            if _sha256_file(database_stage) != validated.database_sha256:
                raise RecoveryError("The staged database hash does not match its manifest.")
            Database(database_stage).validate_health().require_healthy()
            if validated.bundle.settings_present:
                _copy_durable(validated.settings_path, settings_stage)
                if _sha256_file(settings_stage) != validated.settings_sha256:
                    raise RecoveryError("The staged settings hash does not match its manifest.")
                settings_stage.replace(self.settings_path)
            else:
                self.settings_path.unlink(missing_ok=True)

            try:
                for sidecar in self._database_sidecars():
                    if not sidecar.exists() and not sidecar.is_symlink():
                        continue
                    moved = self.data_dir / f".{sidecar.name}.replaced-{uuid4().hex}.tmp"
                    sidecar.replace(moved)
                    moved_sidecars.append((sidecar, moved))
                database_stage.replace(self.database_path)
            except BaseException:
                for original, moved in reversed(moved_sidecars):
                    if moved.exists() or moved.is_symlink():
                        moved.replace(original)
                raise
            for sidecar in self._database_sidecars():
                sidecar.unlink(missing_ok=True)
            for _, moved in moved_sidecars:
                moved.unlink(missing_ok=True)

            if _sha256_file(self.database_path) != validated.database_sha256:
                raise RecoveryError("The restored database hash does not match its manifest.")
            Database(self.database_path).validate_health().require_healthy()
            if validated.bundle.settings_present:
                if _sha256_file(self.settings_path) != validated.settings_sha256:
                    raise RecoveryError("The restored settings hash does not match its manifest.")
            elif self.settings_path.exists() or self.settings_path.is_symlink():
                raise RecoveryError("Settings should be absent after recovery.")
        finally:
            database_stage.unlink(missing_ok=True)
            settings_stage.unlink(missing_ok=True)

    def _database_sidecars(self) -> tuple[Path, Path]:
        return (Path(f"{self.database_path}-wal"), Path(f"{self.database_path}-shm"))

    def _owned_bundle_path(self, path: Path) -> Path:
        if path.is_symlink() or not path.is_dir():
            raise RecoveryError("The recovery bundle path is missing or unsafe.")
        try:
            resolved_backups = self.backups_dir.resolve()
            if path.resolve().parent != resolved_backups:
                raise RecoveryError("The recovery bundle is outside the backup directory.")
        except OSError as exc:
            raise RecoveryError("The recovery bundle path could not be resolved.") from exc
        return path

    @staticmethod
    def _transition(bundle_path: Path, old_state: str, new_state: str) -> None:
        source = bundle_path / old_state
        destination = bundle_path / new_state
        if (
            not source.is_file()
            or source.is_symlink()
            or destination.exists()
            or destination.is_symlink()
        ):
            raise RecoveryError(
                f"Cannot transition recovery state from {old_state} to {new_state}."
            )
        source.replace(destination)


def _safe_relative_path(bundle_path: Path, value: str) -> Path:
    normalised = value.replace("\\", "/")
    posix = PurePosixPath(normalised)
    windows = PureWindowsPath(value)
    if (
        not value
        or posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or ".." in posix.parts
        or ".." in windows.parts
    ):
        raise RecoveryError("A recovery manifest path is unsafe.")
    candidate = bundle_path.joinpath(*posix.parts)
    try:
        candidate.resolve(strict=False).relative_to(bundle_path.resolve())
    except (OSError, ValueError) as exc:
        raise RecoveryError("A recovery manifest path escapes its bundle.") from exc
    return candidate


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _copy_durable(source: Path, destination: Path) -> None:
    with source.open("rb") as input_file, destination.open("xb") as output_file:
        shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
        output_file.flush()
        os.fsync(output_file.fileno())


def _write_atomic(path: Path, value: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
