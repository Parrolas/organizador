"""Current-user Windows login startup registration and Start Menu presence."""

from __future__ import annotations

import logging
import os
import sys
from contextlib import suppress
from pathlib import Path

from organizador import __version__
from organizador.windows_shell import UNINSTALL_KEY, create_shortcut, shortcut_target

try:
    import winreg
except ImportError:  # pragma: no cover - Windows is the shipping platform
    winreg = None  # type: ignore[assignment]

LOGGER = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "Organizador"
SHORTCUT_NAME = "Organizador.lnk"


def startup_command() -> str:
    """Return the command Windows should run after user login."""

    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable)}" --background'
    return f'"{Path(sys.executable)}" -m organizador.main --background'


def set_launch_at_login(enabled: bool) -> None:
    """Create or remove the HKCU Run entry without administrator access."""

    if os.name != "nt" or winreg is None:
        raise OSError("O arranque automático só está disponível no Windows.")
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, startup_command())
        else:
            with suppress(FileNotFoundError):
                winreg.DeleteValue(key, VALUE_NAME)


def is_launch_at_login() -> bool:
    """Return whether the Organizador startup value currently exists."""

    if os.name != "nt" or winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
    except OSError:
        return False
    return bool(value)


def refresh_launch_at_login() -> bool:
    """Point the login entry at the current executable when startup is enabled.

    The registration stores an absolute path; rewriting it on every launch
    keeps a freshly updated installation authoritative without resurrecting
    an entry the user turned off.
    """

    if not getattr(sys, "frozen", False) or os.name != "nt" or winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
    except OSError:
        return False
    desired = startup_command()
    if str(value) == desired:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, desired)
    except OSError:
        LOGGER.warning("Could not refresh the login startup entry", exc_info=True)
        return False
    return True


def refresh_windows_integration() -> bool:
    """Refresh the login entry and Start Menu shortcut for this installation."""

    if os.environ.get("ORGANIZADOR_DISABLE_WINDOWS_INTEGRATION") == "1":
        return False
    refresh_launch_at_login()
    shortcut_ready = ensure_start_menu_shortcut()
    protocol_ready = register_notification_protocol()
    refresh_installed_version()
    return shortcut_ready and protocol_ready


def start_menu_shortcut_path(programs_dir: Path | None = None) -> Path:
    """Return the per-user Start Menu shortcut for the packaged application."""

    if programs_dir is not None:
        return programs_dir / SHORTCUT_NAME
    base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / SHORTCUT_NAME


def ensure_start_menu_shortcut(programs_dir: Path | None = None) -> bool:
    """Create the notification-aware shortcut using COM, without a shell process."""
    if not getattr(sys, "frozen", False) or os.name != "nt":
        return False
    shortcut = start_menu_shortcut_path(programs_dir)
    try:
        create_shortcut(Path(sys.executable), shortcut)
    except Exception:
        LOGGER.warning("Could not create the Start Menu shortcut", exc_info=True)
        return False
    return shortcut.is_file()


def register_notification_protocol() -> bool:
    """Register only a fixed executable command, never a shell or arbitrary target."""
    if not getattr(sys, "frozen", False) or winreg is None:
        return False
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\organizador") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:Organizador")
            winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(
            winreg.HKEY_CURRENT_USER, r"Software\Classes\organizador\shell\open\command"
        ) as key:
            winreg.SetValueEx(
                key,
                "",
                0,
                winreg.REG_SZ,
                f'"{Path(sys.executable)}" --notification-uri "%1"',
            )
    except OSError:
        LOGGER.warning("Could not register notification activation", exc_info=True)
        return False
    return True


def refresh_installed_version() -> None:
    """Update metadata only when the uninstaller owns this exact runtime folder."""
    if not getattr(sys, "frozen", False) or winreg is None:
        return
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, UNINSTALL_KEY, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE
        ) as key:
            root, _ = winreg.QueryValueEx(key, "InstallLocation")
            if (Path(root) / "app").resolve() != Path(sys.executable).resolve().parent:
                return
            winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, __version__)
    except OSError:
        return


def unregister_windows_integration() -> None:
    """Remove registrations only if they still point to this executable."""
    if not getattr(sys, "frozen", False) or winreg is None:
        return
    expected = f'"{Path(sys.executable)}"'
    with suppress(OSError):
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            command, _ = winreg.QueryValueEx(key, VALUE_NAME)
        if command == expected + " --background":
            set_launch_at_login(False)
    protocol = r"Software\Classes\organizador"
    with suppress(OSError):
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, protocol + r"\shell\open\command") as key:
            command, _ = winreg.QueryValueEx(key, "")
        if command == expected + ' --notification-uri "%1"':
            for suffix in (r"\shell\open\command", r"\shell\open", r"\shell", ""):
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, protocol + suffix)
            with suppress(Exception):
                from winrt.windows.ui.notifications import ToastNotificationManager

                from organizador.windows_shell import AUMID

                ToastNotificationManager.history.clear_with_id(AUMID)
    shortcut = start_menu_shortcut_path()
    with suppress(Exception):
        if shortcut_target(shortcut).resolve() == Path(sys.executable).resolve():
            shortcut.unlink()
