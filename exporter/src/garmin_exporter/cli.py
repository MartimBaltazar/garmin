"""CLI entry point.

garmin-exporter login                    # once, interactive, handles MFA
garmin-exporter dump --date 2026-10-28   # raw payloads -> fixtures
garmin-exporter run                      # what the CronJob calls
garmin-exporter backfill --from ... --to ...
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import runner
from .auth import AuthError, interactive_login
from .config import Config
from .logging_setup import configure

log = logging.getLogger(__name__)


EXAMPLES = """\
  garmin-exporter login                     # once, interactive, handles MFA
  garmin-exporter dump --date 2026-10-28    # raw payloads -> fixtures
  garmin-exporter run                       # what the CronJob calls
  garmin-exporter backfill --from 2026-04-01 --to 2026-10-01
"""


def build_parser() -> argparse.ArgumentParser:
    # Globals live on a parent parser so they work on either side of the
    # subcommand: `garmin-exporter --dry-run run` and `... run --dry-run`.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING (default INFO)")
    common.add_argument(
        "--dry-run", action="store_true", help="log samples instead of emitting them"
    )

    parser = argparse.ArgumentParser(
        prog="garmin-exporter",
        description="Pull Garmin Connect data and emit it as metrics.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "login", parents=[common], help="interactive login; writes tokens to the tokenstore"
    )

    dump = sub.add_parser("dump", parents=[common], help="write raw Garmin payloads for one day")
    dump.add_argument("--date", default=None, help="YYYY-MM-DD (default: today UTC)")
    dump.add_argument(
        "--out",
        default="fixtures",
        type=Path,
        help="output directory (default: ./fixtures)",
    )

    run = sub.add_parser(
        "run", parents=[common], help="live run: short lookback window, push to the collector"
    )
    run.add_argument(
        "--lookback-days",
        type=int,
        default=None,
        help="trailing window to refetch (default 2, absorbs late device syncs)",
    )

    back = sub.add_parser(
        "backfill", parents=[common], help="replay history into VictoriaMetrics"
    )
    back.add_argument("--from", dest="start", required=True, help="YYYY-MM-DD (inclusive)")
    back.add_argument("--to", dest="end", default=None, help="YYYY-MM-DD (default: today UTC)")
    back.add_argument("--chunk-days", type=int, default=30)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = Config.from_env()
    except ValueError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    level = args.log_level or cfg.log_level
    configure(level)
    cfg = dataclasses.replace(cfg, log_level=level, dry_run=args.dry_run or cfg.dry_run)

    if args.command == "login":
        try:
            interactive_login(cfg)
        except AuthError as exc:
            log.error("login failed", extra={"error": str(exc)})
            return 1
        return 0

    if args.command == "dump":
        day = args.date or datetime.now(UTC).date().isoformat()
        return runner.run_dump(cfg, day, args.out)

    if args.command == "run":
        if args.lookback_days is not None:
            cfg = dataclasses.replace(cfg, lookback_days=args.lookback_days)
        # Fail before touching Garmin if the emitter is misconfigured: a run that
        # fetches successfully and then cannot emit is worse than no run.
        if cfg.emit in ("vm", "both") and not cfg.vm_url:
            print("config error: GARMIN_EMIT=vm requires GARMIN_VM_URL", file=sys.stderr)
            return 2
        return runner.run_live(cfg)

    if args.command == "backfill":
        end = args.end or datetime.now(UTC).date().isoformat()
        return runner.run_backfill(cfg, args.start, end, args.chunk_days)

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
