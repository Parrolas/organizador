"""Start Menu shortcut registration tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from organizador import startup


def test_shortcut_path_resolution_uses_the_programs_override(tmp_path: Path) -> None:
    assert startup.start_menu_shortcut_path(tmp_path) == tmp_path / "Organizador.lnk"


def test_ensure_shortcut_skips_source_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(startup.sys, "frozen", raising=False)

    created = startup.ensure_start_menu_shortcut(tmp_path)

    assert created is False
    assert not (tmp_path / "Organizador.lnk").exists()


def test_ensure_shortcut_creates_a_real_lnk_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(startup.sys, "frozen", True, raising=False)

    created = startup.ensure_start_menu_shortcut(tmp_path)

    assert created is True
    shortcut = tmp_path / "Organizador.lnk"
    assert shortcut.is_file()
    assert shortcut.read_bytes().startswith(b"\x4c\x00\x00\x00")


def test_ensure_shortcut_refreshes_an_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(startup.sys, "frozen", True, raising=False)
    shortcut = tmp_path / "Organizador.lnk"
    shortcut.write_bytes(b"stale placeholder")

    created = startup.ensure_start_menu_shortcut(tmp_path)

    assert created is True
    assert shortcut.is_file()
    assert shortcut.read_bytes().startswith(b"\x4c\x00\x00\x00")


def test_ensure_shortcut_reports_failure_when_powershell_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(startup.sys, "frozen", True, raising=False)

    class _Failure:
        returncode = 1
        stderr = "access denied"

    def fake_run(*_args: object, **_kwargs: object) -> _Failure:
        return _Failure()

    monkeypatch.setattr(startup.subprocess, "run", fake_run)

    assert startup.ensure_start_menu_shortcut(tmp_path) is False


def test_shortcut_command_escapes_apostrophes_in_paths(tmp_path: Path) -> None:
    target = tmp_path / "Student's Menu" / "App" / "Organizador.exe"
    shortcut = tmp_path / "Student's Menu" / "Organizador.lnk"

    command = startup.shortcut_command(target, shortcut)

    assert "Student's" not in command
    assert "Student''s" in command
    assert command.count("'") % 2 == 0


def test_refresh_launch_at_login_rewrites_a_stale_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(startup.sys, "frozen", True, raising=False)
    values = {"Organizador": '"C:\\instalacao-antiga\\Organizador.exe" --background'}
    written: dict[str, str] = {}

    class _FakeKey:
        def __enter__(self) -> _FakeKey:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def QueryValueEx(self, _key: object, name: str) -> tuple[object, int]:
            return values[name], 1

        def SetValueEx(
            self, _key: object, name: str, _reserved: int, _type: object, value: str
        ) -> None:
            written[name] = value

    class _FakeWinreg:
        HKEY_CURRENT_USER = 2147483649
        KEY_SET_VALUE = 2
        REG_SZ = 1

        def OpenKey(self, _hive: object, _path: str, *_rest: object) -> _FakeKey:
            return _FakeKey()

        def QueryValueEx(self, _key: object, name: str) -> tuple[object, int]:
            return values[name], 1

        def SetValueEx(
            self, _key: object, name: str, _reserved: int, _type: object, value: str
        ) -> None:
            written[name] = value

    fake = _FakeWinreg()
    monkeypatch.setattr(startup, "winreg", fake)

    assert startup.refresh_launch_at_login() is True
    assert written["Organizador"] == startup.startup_command()

    values["Organizador"] = written["Organizador"]
    assert startup.refresh_launch_at_login() is False


def test_refresh_launch_at_login_keeps_a_disabled_entry_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(startup.sys, "frozen", True, raising=False)
    written: dict[str, str] = {}

    class _FakeWinreg:
        HKEY_CURRENT_USER = 2147483649
        KEY_SET_VALUE = 2
        REG_SZ = 1

        def OpenKey(self, _hive: object, _path: str, *_rest: object) -> object:
            raise OSError("value missing")

    class _RecordingWinreg(_FakeWinreg):
        def SetValueEx(
            self, _key: object, name: str, _reserved: int, _type: object, value: str
        ) -> None:
            written[name] = value

    monkeypatch.setattr(startup, "winreg", _RecordingWinreg())

    assert startup.refresh_launch_at_login() is False
    assert written == {}


def test_refresh_windows_integration_refreshes_both_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(startup, "refresh_launch_at_login", lambda: calls.append("run") or True)
    monkeypatch.setattr(
        startup, "ensure_start_menu_shortcut", lambda *args, **kwargs: calls.append("shortcut")
    )

    startup.refresh_windows_integration()

    assert calls == ["run", "shortcut"]
