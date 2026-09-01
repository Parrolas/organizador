"""Startup logging and frozen exception-surface tests."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from organizador.config import AppConfig
from organizador.logging_setup import configure_logging, log_uncaught_exception
from organizador.main import load_config_safely


def test_configure_logging_is_idempotent_for_same_path(tmp_path: Path) -> None:
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        configure_logging(tmp_path)
        configure_logging(tmp_path)
        matching = [
            handler
            for handler in root.handlers
            if isinstance(handler, RotatingFileHandler)
            and Path(handler.baseFilename).resolve() == (tmp_path / "organizador.log").resolve()
        ]
        assert len(matching) == 1
        assert matching[0].maxBytes == 1_500_000
        assert matching[0].backupCount == 2
    finally:
        for handler in list(root.handlers):
            if handler not in before:
                root.removeHandler(handler)
                handler.close()


def test_uncaught_exception_hook_is_safe_without_stderr(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(sys, "stderr", None)
    with caplog.at_level(logging.CRITICAL, logger="organizador.logging_setup"):
        try:
            raise RuntimeError("hidden crash")
        except RuntimeError as exc:
            log_uncaught_exception(type(exc), exc, exc.__traceback__)

    assert "Unhandled exception" in caplog.text
    assert "hidden crash" in caplog.text


def test_uncaught_exception_hook_preserves_console_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delegated: list[BaseException] = []
    monkeypatch.setattr(sys, "stderr", object())
    monkeypatch.setattr(
        sys,
        "__excepthook__",
        lambda _kind, error, _traceback: delegated.append(error),
    )
    error = RuntimeError("visible crash")

    log_uncaught_exception(type(error), error, error.__traceback__)

    assert delegated == [error]


def test_unexpected_settings_failure_uses_safe_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fail_load(_data_dir: Path | None = None) -> AppConfig:
        raise RuntimeError("unexpected settings failure")

    monkeypatch.setattr(AppConfig, "load", fail_load)

    with caplog.at_level(logging.ERROR, logger="organizador.main"):
        config, error = load_config_safely(tmp_path)

    assert config == AppConfig(data_dir=tmp_path)
    assert isinstance(error, RuntimeError)
    assert "unexpected settings failure" in caplog.text
