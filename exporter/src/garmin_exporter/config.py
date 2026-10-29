"""Configuration, entirely from environment variables.

A CronJob has no argv, so environment (or a mounted file) is the only sane
channel. Everything here has a working default except the Garmin credentials,
which are only needed for the first login.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

EMIT_MODES = ("otlp", "vm", "both", "none")

DEFAULT_TOKENSTORE = Path("/var/lib/garmin/tokens")


def _load_env_file() -> Path | None:
    """Read a dotenv file into ``os.environ`` if one is present.

    Rationale: ``cp .env.example .env`` is the first thing anyone does, and it
    exports nothing. Requiring ``set -a; source .env; set +a`` is a footgun that
    fails silently — the run just reports missing credentials.

    Precedence is deliberate: an already-set environment variable always wins, so
    a Kubernetes Secret always beats a stray ``.env`` baked into an image. The
    file is a local-development convenience only.
    """
    explicit = os.environ.get("GARMIN_ENV_FILE")
    candidates = [Path(explicit)] if explicit else [Path.cwd() / ".env", Path.cwd().parent / ".env"]

    for candidate in candidates:
        if not candidate.is_file():
            continue
        for raw_line in candidate.read_text().splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip().removeprefix("export ").strip()
            value = value.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value
        return candidate
    return None


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


@dataclass(frozen=True, slots=True)
class Config:
    email: str | None
    password: str | None
    tokenstore: Path
    emit: str
    otlp_endpoint: str
    otlp_insecure: bool
    vm_url: str | None
    lookback_days: int
    request_pause_seconds: float
    log_level: str
    dry_run: bool = False

    @classmethod
    def from_env(cls) -> Config:
        _load_env_file()
        emit = (_env("GARMIN_EMIT", "otlp") or "otlp").lower()
        if emit not in EMIT_MODES:
            raise ValueError(f"GARMIN_EMIT must be one of {EMIT_MODES}, got {emit!r}")
        return cls(
            email=_env("GARMIN_EMAIL"),
            password=_env("GARMIN_PASSWORD"),
            tokenstore=Path(
                _env("GARMIN_TOKENSTORE", str(DEFAULT_TOKENSTORE)) or DEFAULT_TOKENSTORE
            ),
            emit=emit,
            otlp_endpoint=_env("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
            or "http://localhost:4317",
            otlp_insecure=_env("OTEL_EXPORTER_OTLP_INSECURE", "true") == "true",
            vm_url=_env("GARMIN_VM_URL"),
            lookback_days=_env_int("GARMIN_LOOKBACK_DAYS", 2),
            request_pause_seconds=_env_float("GARMIN_REQUEST_PAUSE_SECONDS", 1.0),
            log_level=(_env("LOG_LEVEL", "INFO") or "INFO").upper(),
        )

    def token_file(self) -> Path:
        return self.tokenstore / "garmin_tokens.json"

    def has_tokens(self) -> bool:
        return self.token_file().exists()

    def state_file(self) -> Path:
        return self.tokenstore / "exporter-state.json"
