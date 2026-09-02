"""Current-user Windows login startup registration and Start Menu presence."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from contextlib import suppress
from pathlib import Path

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


def start_menu_shortcut_path(programs_dir: Path | None = None) -> Path:
    """Return the per-user Start Menu shortcut for the packaged application."""

    if programs_dir is not None:
        return programs_dir / SHORTCUT_NAME
    base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / SHORTCUT_NAME


def ensure_start_menu_shortcut(programs_dir: Path | None = None) -> bool:
    """Create or refresh the Start Menu shortcut so Windows search finds the app.

    Only meaningful for the packaged executable; source runs return False.
    The shortcut is always rewritten so a moved folder or a fresh update
    self-heals on the next launch.
    """

    if not getattr(sys, "frozen", False) or os.name != "nt":
        return False
    target = Path(sys.executable)
    shortcut = start_menu_shortcut_path(programs_dir)
    command = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$s = $ws.CreateShortcut('{shortcut}'); "
        f"$s.TargetPath = '{target}'; "
        f"$s.WorkingDirectory = '{target.parent}'; "
        f"$s.IconLocation = '{target},0'; "
        f"$s.Description = 'Organizador - estudo local'; "
        "$s.Save()"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            LOGGER.warning("Could not create the Start Menu shortcut: %s", result.stderr.strip())
            return False
    except (OSError, subprocess.TimeoutExpired) as exc:
        LOGGER.warning("Could not create the Start Menu shortcut: %s", exc)
        return False
    if not shortcut.is_file():
        return False
    LOGGER.info("Start Menu shortcut ready at %s", shortcut)
    return True
