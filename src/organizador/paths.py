"""Safe Windows path and collision-handling helpers."""

from __future__ import annotations

import ctypes
import os
import re
import shutil
from ctypes import wintypes
from pathlib import Path
from stat import S_ISREG
from typing import Any

from organizador.i18n import _

FileIdentity = tuple[int, int, int, int]

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
DELETE_ACCESS = 0x00010000
FILE_SHARE_READ = 0x00000001
CREATE_NEW = 1
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x00000080
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_RENAME_INFO_CLASS = 3
FILE_DISPOSITION_INFO_CLASS = 4
ERROR_FILE_EXISTS = 80
ERROR_ALREADY_EXISTS = 183
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _FileRenameInfo(ctypes.Structure):
    _fields_ = (
        ("replace_if_exists", wintypes.BOOLEAN),
        ("root_directory", wintypes.HANDLE),
        ("file_name_length", wintypes.DWORD),
        ("file_name", wintypes.WCHAR * 1),
    )


class _FileDispositionInfo(ctypes.Structure):
    _fields_ = (("delete_file", wintypes.BOOLEAN),)


class IncompleteMoveError(OSError):
    """A failed move left an app-created destination that needs recovery."""

    def __init__(self, leftover_path: Path) -> None:
        self.leftover_path = leftover_path
        super().__init__(_("A cópia incompleta ficou em {path}.").format(path=leftover_path))


INVALID_WINDOWS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED_WINDOWS_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def normalise_path_key(path: Path) -> str:
    """Return one stable comparison key for a path and its directory aliases."""

    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError):
        resolved = path.absolute()
    return os.path.normcase(os.path.normpath(os.fspath(resolved)))


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


def move_without_overwrite(
    source: Path,
    target: Path,
    *,
    expected_identity: FileIdentity | None = None,
) -> Path:
    """Move one regular file while atomically refusing an existing destination."""

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    if os.name == "nt":
        _move_windows(source, target, expected_identity)
        return target

    identity = _regular_file_identity(source)
    if expected_identity is not None and identity != expected_identity:
        raise OSError(f"Source changed before moving: {source}")

    try:
        os.link(source, target, follow_symlinks=False)
    except FileExistsError:
        raise
    except OSError:
        _copy_exclusive(source, target, identity)
        return target

    try:
        if _regular_file_identity(source) != identity:
            raise OSError(f"Source changed while moving: {source}")
        if _regular_file_identity(target) != identity:
            raise OSError(f"Destination identity mismatch: {target}")
        source.unlink()
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return target


def _move_windows(
    source: Path,
    target: Path,
    expected_identity: FileIdentity | None,
) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    set_information = kernel32.SetFileInformationByHandle
    set_information.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    set_information.restype = wintypes.BOOL

    source_handle = create_file(
        str(source),
        GENERIC_READ | DELETE_ACCESS,
        FILE_SHARE_READ,
        None,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if source_handle == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error(), str(source))

    destination_handle: int | None = None
    try:
        identity = _regular_file_identity(source)
        if expected_identity is not None and identity != expected_identity:
            raise OSError(f"Source changed before moving: {source}")
        if _rename_windows_handle(source_handle, target, set_information):
            return

        destination_handle = create_file(
            str(target),
            GENERIC_WRITE | DELETE_ACCESS,
            FILE_SHARE_READ,
            None,
            CREATE_NEW,
            FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if destination_handle == INVALID_HANDLE_VALUE:
            destination_handle = None
            error = ctypes.get_last_error()
            if error in {ERROR_FILE_EXISTS, ERROR_ALREADY_EXISTS}:
                raise FileExistsError(target)
            raise ctypes.WinError(error, str(target))

        _copy_windows_handles(kernel32, source_handle, destination_handle, identity[2])
        if not _delete_windows_handle(source_handle, set_information):
            raise ctypes.WinError(ctypes.get_last_error(), str(source))
    except Exception as exc:
        if destination_handle is not None and not _delete_windows_handle(
            destination_handle, set_information
        ):
            raise IncompleteMoveError(target) from exc
        raise
    finally:
        if destination_handle is not None:
            close_handle(destination_handle)
        close_handle(source_handle)


def _rename_windows_handle(
    handle: int,
    target: Path,
    set_information: Any,
) -> bool:
    encoded_name = str(target.absolute()).encode("utf-16-le")
    name_offset = _FileRenameInfo.file_name.offset
    buffer = ctypes.create_string_buffer(ctypes.sizeof(_FileRenameInfo) + len(encoded_name))
    information = ctypes.cast(buffer, ctypes.POINTER(_FileRenameInfo)).contents
    information.replace_if_exists = False
    information.root_directory = None
    information.file_name_length = len(encoded_name)
    ctypes.memmove(ctypes.addressof(buffer) + name_offset, encoded_name, len(encoded_name))
    return bool(set_information(handle, FILE_RENAME_INFO_CLASS, buffer, len(buffer)))


def _delete_windows_handle(handle: int, set_information: Any) -> bool:
    information = _FileDispositionInfo(True)
    return bool(
        set_information(
            handle,
            FILE_DISPOSITION_INFO_CLASS,
            ctypes.byref(information),
            ctypes.sizeof(information),
        )
    )


def _copy_windows_handles(
    kernel32: Any,
    source_handle: int,
    destination_handle: int,
    expected_size: int,
) -> None:
    read_file = kernel32.ReadFile
    read_file.argtypes = (
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    )
    read_file.restype = wintypes.BOOL
    write_file = kernel32.WriteFile
    write_file.argtypes = read_file.argtypes
    write_file.restype = wintypes.BOOL
    flush_file_buffers = kernel32.FlushFileBuffers
    flush_file_buffers.argtypes = (wintypes.HANDLE,)
    flush_file_buffers.restype = wintypes.BOOL

    buffer = ctypes.create_string_buffer(1024 * 1024)
    total = 0
    while True:
        bytes_read = wintypes.DWORD()
        if not read_file(source_handle, buffer, len(buffer), ctypes.byref(bytes_read), None):
            raise ctypes.WinError(ctypes.get_last_error())
        if bytes_read.value == 0:
            break
        written_total = 0
        while written_total < bytes_read.value:
            bytes_written = wintypes.DWORD()
            if not write_file(
                destination_handle,
                ctypes.byref(buffer, written_total),
                bytes_read.value - written_total,
                ctypes.byref(bytes_written),
                None,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if bytes_written.value == 0:
                raise OSError("A escrita do ficheiro de destino não avançou.")
            written_total += bytes_written.value
        total += bytes_read.value
    if total != expected_size:
        raise OSError("O ficheiro mudou durante a cópia.")
    if not flush_file_buffers(destination_handle):
        raise ctypes.WinError(ctypes.get_last_error())


def _copy_exclusive(
    source: Path,
    target: Path,
    identity: FileIdentity,
) -> None:
    created = False
    try:
        with source.open("rb") as input_file:
            if _stat_identity(os.fstat(input_file.fileno())) != identity:
                raise OSError(f"Source changed before copying: {source}")
            with target.open("xb") as output_file:
                created = True
                shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
                output_file.flush()
                os.fsync(output_file.fileno())
            if _stat_identity(os.fstat(input_file.fileno())) != identity:
                raise OSError(f"Source changed while copying: {source}")
            if target.stat().st_size != identity[2]:
                raise OSError(f"Destination size mismatch: {target}")
            shutil.copystat(source, target, follow_symlinks=False)
            if _regular_file_identity(source) != identity:
                raise OSError(f"Source changed before removal: {source}")
            source.unlink()
    except Exception:
        if created:
            target.unlink(missing_ok=True)
        raise


def _regular_file_identity(path: Path) -> FileIdentity:
    try:
        details = path.lstat()
    except OSError as exc:
        raise FileNotFoundError(path) from exc
    if not S_ISREG(details.st_mode):
        raise OSError(f"Not a regular file: {path}")
    return _stat_identity(details)


def _stat_identity(details: os.stat_result) -> FileIdentity:
    return (
        details.st_dev,
        details.st_ino,
        details.st_size,
        details.st_mtime_ns,
    )


def is_direct_child(path: Path, directory: Path) -> bool:
    """Return whether a path belongs directly to a directory."""

    try:
        return path.resolve().parent == directory.resolve()
    except OSError:
        return False


def resolve_contained(path: Path, root: Path) -> Path:
    """Resolve reparse points and require containment within a managed root.

    Junctions, symlinks and ``..`` segments that escape the root raise
    ``OSError`` instead of silently redirecting file operations outside the
    library.
    """

    try:
        resolved = path.resolve()
        anchor = root.resolve()
    except OSError as exc:
        raise OSError(f"Could not resolve path: {path}") from exc
    if resolved != anchor and anchor not in resolved.parents:
        raise OSError(f"Path escapes its managed folder: {path}")
    return resolved
