"""Current-user Windows login startup registration."""

from __future__ import annotations

import os
import sys
from contextlib import suppress
from pathlib import Path

try:
    import winreg
except ImportError:  # pragma: no cover - Windows is the shipping platform
    winreg = None  # type: ignore[assignment]


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "Organizador"


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
