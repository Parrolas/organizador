"""Exercise the exact Setup artifact in an explicitly disposable Windows account.

This intentionally refuses an account with Organizador data or an installation.
Run on a clean Windows CI runner/VM, never the developer's daily-use account.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import tempfile
import time
import winreg
from pathlib import Path

import organizador.updater as updater
from organizador import __version__
from organizador.config import AppConfig, default_data_dir
from organizador.db import Database
from organizador.filer import FilingService
from organizador.windows_shell import UNINSTALL_KEY


def run(executable: Path, *arguments: str, expected: int = 0) -> None:
    result = subprocess.run(
        [str(executable), *arguments], creationflags=subprocess.CREATE_NO_WINDOW, timeout=180
    )
    if result.returncode != expected:
        raise RuntimeError(f"{executable.name} exited {result.returncode}")


def verify_artifact(path: Path) -> None:
    expected = Path(str(path) + ".sha256").read_text(encoding="utf-8").split()[0]
    with path.open("rb") as stream:
        assert hashlib.file_digest(stream, "sha256").hexdigest() == expected


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installer", required=True, type=Path)
    parser.add_argument("--zip", required=True, type=Path)
    parser.add_argument("--disposable-account", action="store_true")
    args = parser.parse_args()
    data = default_data_dir()
    if not args.disposable_account:
        parser.error("This test requires --disposable-account on a clean Windows VM/CI runner")
    if data.exists():
        parser.error("Refusing to test in an account with existing Organizador data")
    for key_name in (UNINSTALL_KEY, r"Software\Classes\organizador"):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_name):
                parser.error(
                    "Refusing to replace an existing Organizador installation/registration"
                )
        except FileNotFoundError:
            pass
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run"
        ) as key:
            winreg.QueryValueEx(key, "Organizador")
        parser.error("Refusing to replace an existing Organizador login entry")
    except FileNotFoundError:
        pass
    verify_artifact(args.installer)
    verify_artifact(args.zip)
    root = Path(tempfile.mkdtemp(prefix="organizador-install-e2e-"))
    install = root / "installed"
    downloads = root / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    config = AppConfig(
        data_dir=data,
        downloads_dir=downloads,
        university_root=root / "coursework",
        initialized=True,
        check_updates_on_launch=False,
        watch_enabled=False,
    )
    config.ensure_directories()
    config.save()
    database = Database(config.database_path)
    database.initialize()
    filer = FilingService(config, database)
    subject = database.add_subject("Test subject", "TEST", "#087A74", (), "TEST")
    filer.ensure_subject_structure(subject)
    original = config.downloads_dir / "protected.txt"
    original.write_bytes(b"preserve coursework contents" * 20)
    inbox = filer.ingest(original)
    assert inbox is not None
    document = filer.file_document(inbox.id, subject.id, "Outros", original.name)
    original_bytes = document.current_path.read_bytes()
    # Simulate the additive migration from the prior portable catalog.
    with database.connect() as connection:
        connection.execute("DROP TABLE notification_actions")
        connection.commit()
    print(f"Evidence sandbox: {root}", flush=True)
    parameters = ("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", f"/DIR={install}")
    run(args.installer.resolve(), *parameters, f"/LOG={root / 'install.log'}")
    uninstaller = install / "unins000.exe"
    assert uninstaller.is_file()
    executable = install / "app" / "Organizador.exe"
    run(executable, "--smoke-test", "--data-dir", str(data))
    assert database.inspect_schema().is_current
    run(args.installer.resolve(), *parameters, f"/LOG={root / 'reinstall.log'}")
    assert document.current_path.read_bytes() == original_bytes

    transaction = updater.create_update_transaction(
        executable.parent,
        __version__,
        data_dir=data,
        old_pid=2_147_483_647,
    )
    updater.extract_to_staging(args.zip.resolve(), transaction.staging_dir)
    marker = transaction.staging_dir / "added-by-update.txt"
    marker.write_text("managed update file", encoding="utf-8")
    with (transaction.staging_dir / "runtime-files.txt").open("a", encoding="utf-8") as stream:
        stream.write("added-by-update.txt\n")
    updater.write_update_helper(transaction)
    helper = updater.launch_update_helper(transaction)
    new_pid: int | None = None
    try:
        assert helper.wait(timeout=180) == 0
        result = updater.read_update_result(transaction)
        assert result is not None
        new_pid = result.new_pid
        assert result.status is updater.UpdateResultStatus.SUCCEEDED
        assert uninstaller.is_file()
        assert (executable.parent / marker.name).is_file()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
            assert winreg.QueryValueEx(key, "DisplayVersion")[0] == __version__
    finally:
        if helper.poll() is None:
            helper.terminate()
        if new_pid is not None:
            # Only the idle fixture app launched by this transaction; no transfers pending.
            subprocess.run(
                ["taskkill.exe", "/PID", str(new_pid), "/F"],
                creationflags=subprocess.CREATE_NO_WINDOW,
                capture_output=True,
                check=False,
            )
            time.sleep(1)
    run(
        uninstaller,
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        f"/LOG={root / 'uninstall.log'}",
    )
    assert not (executable.parent / marker.name).exists()
    assert not executable.exists()
    assert document.current_path.read_bytes() == original_bytes
    assert config.database_path.is_file()
    assert AppConfig.load(data).university_root == config.university_root
    run(args.installer.resolve(), *parameters, f"/LOG={root / 'restore-install.log'}")
    run(executable, "--smoke-test", "--data-dir", str(data))
    assert database.get_file(document.id) is not None
    run(uninstaller, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")
    print("PASS: install, migration, reinstall, real update, uninstall and data reuse", flush=True)
    print(f"Fixture data and logs retained in disposable account: {root}", flush=True)


if __name__ == "__main__":
    main()
