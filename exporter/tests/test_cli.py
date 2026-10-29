"""CLI-level tests.

These exist because of a specific failure: with no credentials, ``dump`` raised
``AuthError`` into a traceback instead of explaining what to fix. The exit codes
matter, but the point of these tests is that the failure is *handled* — no
exception escapes ``main``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from garmin_exporter.cli import main

MANAGED = (
    "GARMIN_EMAIL",
    "GARMIN_PASSWORD",
    "GARMIN_TOKENSTORE",
    "GARMIN_EMIT",
    "GARMIN_ENV_FILE",
    "GARMIN_VM_URL",
)


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No ambient credentials, no ambient .env, no ambient token store."""
    for name in MANAGED:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


def test_config_prints_resolved_settings(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["config"]) == 0
    out = capsys.readouterr().out
    assert "token store" in out
    assert "authentication is not configured" in out


def test_dump_without_credentials_exits_2_and_prints_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["dump", "--date", "2026-10-28", "--out", "fixtures"]) == 2
    err = capsys.readouterr().err
    assert "Cannot authenticate to Garmin Connect" in err
    assert "GARMIN_EMAIL" in err


def test_dump_without_credentials_leaves_no_empty_directory(tmp_path: Path) -> None:
    """A failed dump must not look like a successful one."""
    main(["dump", "--date", "2026-10-28", "--out", "fixtures"])
    assert not (tmp_path / "fixtures").exists()


def test_login_without_credentials_exits_1_without_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["login"]) == 1
    err = capsys.readouterr().err
    assert "set GARMIN_EMAIL and GARMIN_PASSWORD" in err


def test_run_without_credentials_fails_fast(capsys: pytest.CaptureFixture[str]) -> None:
    """A CronJob pod gets the same actionable message, not a traceback."""
    assert main(["run", "--dry-run"]) == 2
    assert "Cannot authenticate" in capsys.readouterr().err


def test_emit_vm_without_url_is_rejected_before_any_network_call(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GARMIN_EMAIL", "a@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "secret")
    monkeypatch.setenv("GARMIN_EMIT", "vm")
    monkeypatch.delenv("GARMIN_VM_URL", raising=False)

    assert main(["run"]) == 2
    assert "GARMIN_VM_URL" in capsys.readouterr().err


def test_invalid_emit_mode_exits_2_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GARMIN_EMIT", "carrier-pigeon")

    assert main(["config"]) == 2
    assert "GARMIN_EMIT" in capsys.readouterr().err
