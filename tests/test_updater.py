"""Self-update module tests: versioning, fetching, verification, extraction."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from organizador import updater
from organizador.updater import UpdaterError


class _FakeResponse:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self._data) - self._pos
        chunk = self._data[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _make_zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Organizador.exe", b"app binary")
        archive.writestr("_internal/placeholder.txt", b"internal data")
    return buffer.getvalue()


def _fake_release(tag: str, *, with_assets: bool = True) -> bytes:
    assets = []
    if with_assets:
        assets = [
            {
                "name": f"Organizador-{tag}-windows-x64.zip",
                "browser_download_url": f"https://example.invalid/{tag}.zip",
            },
            {
                "name": f"Organizador-{tag}-windows-x64.zip.sha256",
                "browser_download_url": f"https://example.invalid/{tag}.zip.sha256",
            },
        ]
    return json.dumps({"tag_name": f"v{tag}", "assets": assets}).encode("utf-8")


def test_version_tuple_parsing() -> None:
    assert updater.version_tuple("v0.5.0") == (0, 5, 0)
    assert updater.version_tuple("0.6.0") == (0, 6, 0)
    assert updater.version_tuple("v0.5.0-alpha") is None
    assert updater.version_tuple("not-a-version") is None


def _newer_version() -> str:
    current = updater.version_tuple(updater.__version__)
    assert current is not None
    return ".".join(str(part) for part in (current[0], current[1], current[2] + 1))


def test_fetch_latest_release_returns_newer_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_version = _newer_version()

    def fake_urlopen(request: object, **kwargs: object) -> _FakeResponse:
        assert isinstance(request, object)
        return _FakeResponse(_fake_release(fake_version))

    monkeypatch.setattr(updater.urllib.request, "urlopen", fake_urlopen)

    info = updater.fetch_latest_release()

    assert info is not None
    assert info.version == tuple(int(part) for part in fake_version.split("."))
    assert f"{fake_version}.zip" in info.zip_url


def test_fetch_latest_release_ignores_older_or_equal_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        updater.urllib.request,
        "urlopen",
        lambda request, **kwargs: _FakeResponse(_fake_release(updater.__version__)),
    )
    assert updater.fetch_latest_release() is None

    monkeypatch.setattr(
        updater.urllib.request,
        "urlopen",
        lambda request, **kwargs: _FakeResponse(_fake_release("0.0.1")),
    )
    assert updater.fetch_latest_release() is None


def test_fetch_latest_release_handles_missing_assets_and_network_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        updater.urllib.request,
        "urlopen",
        lambda request, **kwargs: _FakeResponse(_fake_release("0.6.0", with_assets=False)),
    )
    assert updater.fetch_latest_release() is None

    def raise_error(_request: object, **kwargs: object) -> _FakeResponse:
        raise OSError("offline")

    monkeypatch.setattr(updater.urllib.request, "urlopen", raise_error)
    assert updater.fetch_latest_release() is None


def test_download_and_verify_accepts_matching_checksum(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = _make_zip_bytes()
    import hashlib

    digest = hashlib.sha256(payload).hexdigest()
    bodies = {
        "https://example.invalid/app.zip": payload,
        "https://example.invalid/app.zip.sha256": f"{digest}  app.zip".encode(),
    }

    def fake_urlopen(request: object, **kwargs: object) -> _FakeResponse:
        url = str(getattr(request, "full_url", ""))
        return _FakeResponse(bodies[url])

    monkeypatch.setattr(updater.urllib.request, "urlopen", fake_urlopen)

    zip_path = updater.download_and_verify(
        "https://example.invalid/app.zip",
        "https://example.invalid/app.zip.sha256",
        tmp_path,
    )

    assert zip_path.is_file()
    assert zip_path.read_bytes() == payload


def test_download_and_verify_rejects_a_mismatched_checksum(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = _make_zip_bytes()

    def fake_urlopen(request: object, **kwargs: object) -> _FakeResponse:
        url = str(getattr(request, "full_url", ""))
        if url.endswith(".sha256"):
            return _FakeResponse(b"0" * 64 + b"  app.zip")
        return _FakeResponse(payload)

    monkeypatch.setattr(updater.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(UpdaterError, match="verificação"):
        updater.download_and_verify(
            "https://example.invalid/app.zip",
            "https://example.invalid/app.zip.sha256",
            tmp_path,
        )

    assert not (tmp_path / "organizador-update.zip").exists()


def test_extract_to_staging_validates_the_application_layout(tmp_path: Path) -> None:
    zip_path = tmp_path / "app.zip"
    zip_path.write_bytes(_make_zip_bytes())
    staging = tmp_path / "staging"

    result = updater.extract_to_staging(zip_path, staging)

    assert (result / "Organizador.exe").is_file()
    assert (result / "_internal").is_dir()
    assert not zip_path.exists()

    bad_buffer = io.BytesIO()
    with zipfile.ZipFile(bad_buffer, "w") as archive:
        archive.writestr("Organizador.exe", b"app binary")
    bad_zip = tmp_path / "bad.zip"
    bad_zip.write_bytes(bad_buffer.getvalue())
    with pytest.raises(UpdaterError, match="aplicação completa"):
        updater.extract_to_staging(bad_zip, tmp_path / "bad-staging")


def test_write_swap_script_contains_the_full_swap_sequence(tmp_path: Path) -> None:
    app_dir = tmp_path / "Organizador"
    staging = tmp_path / "Organizador.update"
    app_dir.mkdir()
    staging.mkdir()

    script = updater.write_swap_script(app_dir, staging)

    content = script.read_text(encoding="utf-8")
    assert f'ren "{app_dir}" "Organizador.old"' in content
    assert f'move /y "{staging}" "{app_dir}" >nul' in content
    assert f'start "" "{app_dir}\\Organizador.exe" --background' in content
    assert b"\r\n" in script.read_bytes()


def test_launch_swap_invokes_cmd_detached(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    script = tmp_path / "update.cmd"
    script.write_text("@echo off", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_popen(args: list[str], **kwargs: object) -> object:
        calls.append(args)
        return object()

    monkeypatch.setattr(updater.subprocess, "Popen", fake_popen)

    updater.launch_swap(script)

    assert calls and calls[0][-2:] == ["/c", str(script)]


def test_app_directory_detects_frozen_layout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app_dir = tmp_path / "Organizador"
    (app_dir / "_internal").mkdir(parents=True)

    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater.sys, "executable", str(app_dir / "Organizador.exe"))
    assert updater.app_directory() == app_dir

    monkeypatch.setattr(updater.sys, "frozen", False, raising=False)
    assert updater.app_directory() is None


def test_cleanup_previous_version_removes_the_rollback_folder(tmp_path: Path) -> None:
    app_dir = tmp_path / "Organizador"
    old_dir = tmp_path / "Organizador.old"
    app_dir.mkdir()
    old_dir.mkdir()
    (old_dir / "Organizador.exe").write_bytes(b"old")

    updater.cleanup_previous_version(app_dir)

    assert not old_dir.exists()
