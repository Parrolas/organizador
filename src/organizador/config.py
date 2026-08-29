"""Application configuration and Windows known-folder discovery."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

try:
    import winreg
except ImportError:  # pragma: no cover - only relevant outside Windows
    winreg = None  # type: ignore[assignment]


APP_NAME = "Organizador"
DOWNLOADS_GUID = "{374DE290-123F-4565-9164-39C4925E467B}"
MANUAL_IMPORT_BATCH_LIMIT = 25
DEFAULT_EXTENSIONS = (
    ".pdf",
    ".doc",
    ".docx",
    ".ppt",
    ".pptx",
    ".xls",
    ".xlsx",
    ".ipynb",
    ".md",
    ".txt",
    ".csv",
    ".one",
)
TEMPORARY_SUFFIXES = (
    ".crdownload",
    ".part",
    ".partial",
    ".tmp",
    ".download",
    ".opdownload",
)


def _known_folder(value_name: str, fallback: Path) -> Path:
    """Resolve a Windows user-shell folder while retaining a safe fallback."""

    if winreg is None:
        return fallback
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            value, _ = winreg.QueryValueEx(key, value_name)
    except OSError:
        return fallback
    return Path(os.path.expandvars(str(value))).expanduser()


def default_data_dir() -> Path:
    """Return the per-user application-data directory."""

    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return base / APP_NAME


def default_downloads_dir() -> Path:
    """Return the configured Windows Downloads known folder."""

    return _known_folder(DOWNLOADS_GUID, Path.home() / "Downloads")


def default_documents_dir() -> Path:
    """Return the configured Windows Documents known folder."""

    return _known_folder("Personal", Path.home() / "Documents")


@dataclass(slots=True)
class AppConfig:
    """Persistent application settings."""

    data_dir: Path = field(default_factory=default_data_dir)
    university_root: Path = field(default_factory=lambda: default_documents_dir() / "Universidade")
    downloads_dir: Path = field(default_factory=default_downloads_dir)
    allowed_extensions: tuple[str, ...] = DEFAULT_EXTENSIONS
    minimum_file_size: int = 512
    watch_enabled: bool = True
    launch_at_login: bool = False
    prompt_timeout_seconds: int = 45
    initialized: bool = False

    @property
    def settings_path(self) -> Path:
        """Path to the JSON settings file."""

        return self.data_dir / "settings.json"

    @property
    def database_path(self) -> Path:
        """Path to the local SQLite database."""

        return self.data_dir / "organizador.db"

    @property
    def log_path(self) -> Path:
        """Path to the rotating application log."""

        return self.data_dir / "organizador.log"

    @property
    def inbox_dir(self) -> Path:
        """Folder holding downloaded files awaiting classification."""

        return self.university_root / "_Caixa de Entrada"

    def ensure_directories(self) -> None:
        """Create application and university directories if absent."""

        self.validate()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.university_root.mkdir(parents=True, exist_ok=True)
        self.inbox_dir.mkdir(parents=True, exist_ok=True)

    def validate(self) -> None:
        """Reject settings that could cause the watcher to reprocess its own files."""

        downloads = self.downloads_dir.resolve()
        university = self.university_root.resolve()
        if (
            university == downloads
            or downloads in university.parents
            or university in downloads.parents
        ):
            raise ValueError(
                "As pastas Universidade e Downloads não podem coincidir nem "
                "ficar uma dentro da outra."
            )
        if self.minimum_file_size < 0:
            raise ValueError("O tamanho mínimo não pode ser negativo.")
        if self.prompt_timeout_seconds < 10:
            raise ValueError("O tempo do popup deve ser de pelo menos 10 segundos.")

    def accepts(self, path: Path) -> bool:
        """Return whether a file has an eligible, non-temporary suffix."""

        lowered = path.name.casefold()
        if any(lowered.endswith(suffix) for suffix in TEMPORARY_SUFFIXES):
            return False
        return path.suffix.casefold() in self.allowed_extensions

    def save(self) -> None:
        """Persist settings atomically to avoid a partially written JSON file."""

        self.validate()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        payload = asdict(self)
        payload["data_dir"] = str(self.data_dir)
        payload["university_root"] = str(self.university_root)
        payload["downloads_dir"] = str(self.downloads_dir)
        payload["allowed_extensions"] = list(self.allowed_extensions)
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        file_descriptor, temporary_name = tempfile.mkstemp(
            dir=self.data_dir, prefix="settings-", suffix=".tmp", text=True
        )
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
            Path(temporary_name).replace(self.settings_path)
        finally:
            Path(temporary_name).unlink(missing_ok=True)

    @classmethod
    def load(cls, data_dir: Path | None = None) -> AppConfig:
        """Load settings, returning defaults before first launch."""

        target_dir = data_dir or default_data_dir()
        settings_path = target_dir / "settings.json"
        if not settings_path.exists():
            return cls(data_dir=target_dir)
        try:
            raw: dict[str, Any] = json.loads(settings_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError(
                f"Não foi possível ler as definições em {settings_path}: {exc}"
            ) from exc
        extensions = tuple(
            _normalise_extension(str(value)) for value in raw.get("allowed_extensions", [])
        )
        return cls(
            data_dir=target_dir,
            university_root=Path(
                raw.get("university_root", default_documents_dir() / "Universidade")
            ),
            downloads_dir=Path(raw.get("downloads_dir", default_downloads_dir())),
            allowed_extensions=extensions or DEFAULT_EXTENSIONS,
            minimum_file_size=int(raw.get("minimum_file_size", 512)),
            watch_enabled=bool(raw.get("watch_enabled", True)),
            launch_at_login=bool(raw.get("launch_at_login", False)),
            prompt_timeout_seconds=int(raw.get("prompt_timeout_seconds", 45)),
            initialized=bool(raw.get("initialized", False)),
        )


def _normalise_extension(value: str) -> str:
    """Normalise one user-entered file extension."""

    cleaned = re.sub(r"\s+", "", value.casefold())
    if not cleaned:
        return ""
    return cleaned if cleaned.startswith(".") else f".{cleaned}"


def parse_extensions(value: str) -> tuple[str, ...]:
    """Parse comma, semicolon or whitespace-separated extensions."""

    extensions = {_normalise_extension(item) for item in re.split(r"[,;\s]+", value)}
    extensions.discard("")
    return tuple(sorted(extensions))
