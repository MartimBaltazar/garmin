"""Dotenv loading tests.

This is the layer that made `cp .env.example .env` silently do nothing, so it is
worth testing directly rather than only through the CLI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from garmin_exporter.config import Config, loaded_env_file

MANAGED = (
    "GARMIN_EMAIL",
    "GARMIN_PASSWORD",
    "GARMIN_TOKENSTORE",
    "GARMIN_EMIT",
    "GARMIN_ENV_FILE",
    "GARMIN_VM_URL",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in MANAGED:
        monkeypatch.delenv(name, raising=False)


def write_env(path: Path, body: str) -> None:
    path.write_text(body)


def test_dotenv_in_cwd_is_loaded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    write_env(tmp_path / ".env", "GARMIN_EMAIL=a@example.com\nGARMIN_PASSWORD=secret\n")
    monkeypatch.chdir(tmp_path)

    cfg = Config.from_env()

    assert cfg.email == "a@example.com"
    assert cfg.password == "secret"
    assert loaded_env_file() == tmp_path / ".env"


def test_dotenv_in_parent_is_loaded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Running from exporter/ must still find a repo-root .env."""
    write_env(tmp_path / ".env", "GARMIN_EMAIL=parent@example.com\n")
    child = tmp_path / "exporter"
    child.mkdir()
    monkeypatch.chdir(child)

    assert Config.from_env().email == "parent@example.com"


def test_real_environment_wins_over_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A Kubernetes Secret must beat a .env baked into an image."""
    write_env(tmp_path / ".env", "GARMIN_EMAIL=file@example.com\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GARMIN_EMAIL", "secret@example.com")

    assert Config.from_env().email == "secret@example.com"


def test_explicit_env_file_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    write_env(tmp_path / ".env", "GARMIN_EMAIL=wrong@example.com\n")
    explicit = tmp_path / "custom.env"
    write_env(explicit, "GARMIN_EMAIL=right@example.com\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GARMIN_ENV_FILE", str(explicit))

    assert Config.from_env().email == "right@example.com"


def test_export_prefix_quotes_and_comments_are_parsed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    write_env(
        tmp_path / ".env",
        "\n".join(
            [
                "# a comment",
                "",
                "export GARMIN_EMAIL='quoted@example.com'",
                'GARMIN_PASSWORD="double@quoted"',
                "NOT_AN_ASSIGNMENT",
            ]
        ),
    )
    monkeypatch.chdir(tmp_path)

    cfg = Config.from_env()

    assert cfg.email == "quoted@example.com"
    assert cfg.password == "double@quoted"


def test_missing_dotenv_is_not_an_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)

    cfg = Config.from_env()

    assert cfg.email is None
    assert loaded_env_file() is None


def test_empty_value_counts_as_unset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The example file ships `GARMIN_EMAIL=` — that must read as 'not set'."""
    write_env(tmp_path / ".env", "GARMIN_EMAIL=\nGARMIN_PASSWORD=\n")
    monkeypatch.chdir(tmp_path)

    cfg = Config.from_env()

    assert cfg.email is None
    assert cfg.password is None


def test_invalid_emit_mode_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    write_env(tmp_path / ".env", "GARMIN_EMIT=carrier-pigeon\n")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="GARMIN_EMIT"):
        Config.from_env()
