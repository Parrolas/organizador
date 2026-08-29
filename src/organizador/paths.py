"""Safe Windows path and collision-handling helpers."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

INVALID_WINDOWS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED_WINDOWS_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def sanitise_component(value: str, *, fallback: str = "Sem nome", limit: int = 120) -> str:
    """Return a valid Windows folder or filename component."""

    cleaned = INVALID_WINDOWS_CHARS.sub("-", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        cleaned = fallback
    if cleaned.partition(".")[0].upper() in RESERVED_WINDOWS_NAMES:
        cleaned = f"{cleaned}_"
    return cleaned[:limit].rstrip(" .") or fallback


def sanitise_filename(value: str, *, fallback: str = "Documento") -> str:
    """Strip traversal and make a user-provided filename safe."""

    basename = Path(value).name
    suffix = Path(basename).suffix
    stem = basename[: -len(suffix)] if suffix else basename
    safe_stem = sanitise_component(stem, fallback=fallback, limit=170)
    safe_suffix = re.sub(r"[^A-Za-z0-9.]", "", suffix)[:20]
    return f"{safe_stem}{safe_suffix}"


def unique_path(directory: Path, filename: str) -> Path:
    """Return a non-existing path without overwriting any user file."""

    safe_name = sanitise_filename(filename)
    candidate = directory / safe_name
    if not candidate.exists():
        return candidate
    suffix = candidate.suffix
    stem = candidate.stem
    counter = 2
    while True:
        alternate = directory / f"{stem} ({counter}){suffix}"
        if not alternate.exists():
            return alternate
        counter += 1


def move_without_overwrite(source: Path, target: Path) -> Path:
    """Move a file to an exact, pre-checked destination."""

    if not source.is_file():
        raise FileNotFoundError(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(target)
    shutil.move(str(source), str(target))
    return target


def is_direct_child(path: Path, directory: Path) -> bool:
    """Return whether a path belongs directly to a directory."""

    try:
        return path.resolve().parent == directory.resolve()
    except OSError:
        return False
