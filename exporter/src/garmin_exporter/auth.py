"""Authentication and token lifecycle.

The library (v0.3.x) stores DI OAuth tokens at ``<tokenstore>/garmin_tokens.json``
and refreshes them automatically. Two consequences that shape this module:

1. The CronJob **never** sees the password. It resumes from the token file.
   ``GARMIN_EMAIL``/``GARMIN_PASSWORD`` exist only for the one-off login.
2. MFA cannot be answered by a CronJob. It is handled interactively by
   ``garmin-exporter login``, which is why that subcommand exists separately.
"""

from __future__ import annotations

import logging
from typing import Any

from garminconnect import Garmin

from .config import Config

log = logging.getLogger(__name__)


class AuthError(RuntimeError):
    """Authentication could not be completed without human interaction."""


def interactive_login(cfg: Config) -> Garmin:
    """One-off login. The library prompts for an MFA code on the terminal."""
    if not (cfg.email and cfg.password):
        raise AuthError("set GARMIN_EMAIL and GARMIN_PASSWORD to log in")
    cfg.tokenstore.mkdir(parents=True, exist_ok=True)
    log.info("logging in as %s (tokenstore=%s)", cfg.email, cfg.tokenstore)
    client = Garmin(cfg.email, cfg.password)
    client.login(str(cfg.tokenstore))
    log.info("login ok; tokens written to %s", cfg.token_file())
    return client


def get_client(cfg: Config) -> Garmin:
    """Return an authenticated client, preferring cached tokens.

    Raises AuthError rather than attempting an interactive flow, because the
    caller is a CronJob with no terminal and no credentials.
    """
    if cfg.has_tokens():
        client = Garmin()
        try:
            client.login(str(cfg.tokenstore))
            log.debug("resumed session from %s", cfg.token_file())
            return client
        except Exception as exc:  # noqa: BLE001 - library raises a wide range
            log.warning("cached tokens rejected (%s); trying credentials", exc)

    if not (cfg.email and cfg.password):
        raise AuthError(
            "no usable cached tokens and no GARMIN_EMAIL/GARMIN_PASSWORD. "
            "Run `garmin-exporter login` once and persist the tokenstore."
        )

    cfg.tokenstore.mkdir(parents=True, exist_ok=True)
    client = Garmin(cfg.email, cfg.password, return_on_mfa=True)
    status: Any = client.login(str(cfg.tokenstore))
    if status == "needs_mfa":
        raise AuthError(
            "Garmin requires an MFA code for this account; a CronJob cannot answer it. "
            "Run `garmin-exporter login` interactively once, then mount the tokenstore."
        )
    return client
