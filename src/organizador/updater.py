"""Self-update support for the packaged Windows application.

The update lifecycle is: fetch the latest GitHub release, download the ZIP,
verify its SHA-256 against the published checksum, extract into a staging
folder next to the application, then swap folders through a detached script
that waits for the app to exit. User data in %LOCALAPPDATA% is never touched.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from organizador import __version__
from organizador.i18n import _

LOGGER = logging.getLogger(__name__)

RELEASES_API = "https://api.github.com/repos/Parrolas/organizador/releases/latest"
REQUEST_TIMEOUT = 10.0
DOWNLOAD_TIMEOUT = 60.0


class UpdaterError(RuntimeError):
    """A recoverable update failure suitable for display to the user."""


@dataclass(frozen=True, slots=True)
class UpdateInfo:
    """One newer published release ready to download."""

    version: tuple[int, ...]
    tag_name: str
    zip_url: str
    sha256_url: str


def is_frozen() -> bool:
    """Return whether the app runs from the packaged executable."""

    return bool(getattr(sys, "frozen", False))


def version_tuple(tag: str) -> tuple[int, ...] | None:
    """Parse a v-prefixed release tag into comparable integers."""

    cleaned = tag[1:] if tag.startswith("v") else tag
    try:
        parsed = tuple(int(part) for part in cleaned.split("."))
    except ValueError:
        return None
    return parsed if parsed else None


def app_directory() -> Path | None:
    """Return the packaged application folder, or None when running from source."""

    if not is_frozen():
        return None
    candidate = Path(sys.executable).resolve().parent
    if candidate.name == "Organizador" and (candidate / "_internal").is_dir():
        return candidate
    return None


def staging_directory(app_dir: Path) -> Path:
    """Return the sibling folder used to stage an incoming update."""

    return app_dir.parent / "Organizador.update"


def fetch_latest_release() -> UpdateInfo | None:
    """Return the newest published release when it is newer than the running app."""

    request = urllib.request.Request(
        RELEASES_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Organizador",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        LOGGER.info("Update check failed: %s", exc)
        return None

    remote = version_tuple(str(payload.get("tag_name", "")))
    current = version_tuple(__version__)
    if remote is None or current is None or remote <= current:
        return None

    zip_asset: dict[str, object] | None = None
    sha_asset: dict[str, object] | None = None
    for asset in payload.get("assets", []):
        name = str(asset.get("name", ""))
        if name.endswith("-windows-x64.zip") and zip_asset is None:
            zip_asset = asset
        elif name.endswith("-windows-x64.zip.sha256"):
            sha_asset = asset
    if zip_asset is None or sha_asset is None:
        return None
    return UpdateInfo(
        remote,
        str(payload["tag_name"]),
        str(zip_asset["browser_download_url"]),
        str(sha_asset["browser_download_url"]),
    )


def download_and_verify(download_url: str, sha256_url: str, destination_dir: Path) -> Path:
    """Download the release ZIP and discard it unless its SHA-256 matches."""

    destination_dir.mkdir(parents=True, exist_ok=True)
    zip_path = destination_dir / "organizador-update.zip"
    try:
        with (
            urllib.request.urlopen(
                urllib.request.Request(download_url, headers={"User-Agent": "Organizador"}),
                timeout=DOWNLOAD_TIMEOUT,
            ) as response,
            zip_path.open("wb") as handle,
        ):
            shutil.copyfileobj(response, handle, length=1024 * 1024)
    except Exception as exc:
        zip_path.unlink(missing_ok=True)
        raise UpdaterError(
            _("A transferência da atualização falhou: {error}").format(error=exc)
        ) from exc

    try:
        with urllib.request.urlopen(
            urllib.request.Request(sha256_url, headers={"User-Agent": "Organizador"}),
            timeout=REQUEST_TIMEOUT,
        ) as response:
            sha_text = response.read().decode("utf-8", errors="replace")
    except Exception as exc:
        zip_path.unlink(missing_ok=True)
        raise UpdaterError(
            _("Não foi possível verificar a atualização: {error}").format(error=exc)
        ) from exc

    expected = sha_text.split()[0].strip() if sha_text.split() else ""
    digest = hashlib.sha256()
    with zip_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    if not expected or expected.lower() != digest.hexdigest().lower():
        zip_path.unlink(missing_ok=True)
        raise UpdaterError(_("A verificação da atualização falhou; o ficheiro foi descartado."))
    return zip_path


def extract_to_staging(zip_path: Path, staging_dir: Path) -> Path:
    """Extract the verified ZIP and validate the application layout."""

    if staging_dir.exists():
        shutil.rmtree(staging_dir, ignore_errors=True)
    staging_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(staging_dir)
    except zipfile.BadZipFile as exc:
        raise UpdaterError(_("A atualização descarregada está corrompida.")) from exc
    finally:
        zip_path.unlink(missing_ok=True)
    if not (staging_dir / "Organizador.exe").is_file() or not (staging_dir / "_internal").is_dir():
        raise UpdaterError(_("A atualização não contém a aplicação completa."))
    return staging_dir


def write_swap_script(app_dir: Path, staging_dir: Path) -> Path:
    """Write the detached swap script that runs once the app has exited."""

    old_dir = app_dir.parent / "Organizador.old"
    script = app_dir.parent / "organizador-update.cmd"
    lines = [
        "@echo off",
        "timeout /t 2 /nobreak >nul",
        f'if exist "{old_dir}" rmdir /s /q "{old_dir}"',
        f'ren "{app_dir}" "Organizador.old"',
        f'move /y "{staging_dir}" "{app_dir}" >nul',
        f'if not exist "{app_dir}\\Organizador.exe" move /y "{old_dir}" "{app_dir}" >nul',
        f'start "" "{app_dir}\\Organizador.exe" --background',
    ]
    script.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    return script


def launch_swap(script_path: Path) -> None:
    """Run the swap script fully detached from the exiting application."""

    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    subprocess.Popen(
        [os.environ.get("COMSPEC", "cmd.exe"), "/c", str(script_path)],
        creationflags=flags,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def cleanup_previous_version(app_dir: Path) -> None:
    """Remove the rollback folder once the current version has launched."""

    old_dir = app_dir.parent / "Organizador.old"
    try:
        if old_dir.is_dir():
            shutil.rmtree(old_dir)
            LOGGER.info("Removed previous version folder %s", old_dir)
    except OSError:
        LOGGER.exception("Could not remove previous version folder %s", old_dir)


def download_temp_directory() -> Path:
    """Return the per-session folder used for the verified ZIP."""

    folder = Path(tempfile.gettempdir()) / "organizador-update"
    folder.mkdir(parents=True, exist_ok=True)
    return folder
