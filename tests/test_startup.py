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
