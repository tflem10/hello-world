"""``swing`` command-line entry point.

Sub-command implementations are imported lazily so that ``swing --help`` and
``swing kill`` work even if a heavy optional dependency is broken.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import ConfigError, load_config
from .logging_setup import get_logger, setup_logging

log = get_logger("swing.cli")

EPILOG = """\
typical flow:
  swing doctor                    is this machine able to run the system?
  swing universe --refresh        rebuild the tradable universe from the CSVs
  swing data --backfill           populate the local price cache
  swing backtest --walk-forward   REQUIRED before any live picks
  swing scan --dry-run            produce a pick sheet without sending alerts
  swing scan                      nightly picks -> phone/email/desktop
  swing confirm                   pre-open re-quote of last night's picks
  swing execute --live            v2: place the drafted orders (guarded)
  swing journal add AAPL 10 190.50 182.00   record a hand-placed trade
  swing positions                 what the journal says you hold
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="swing",
        description="Evidence-based swing-trading pick system (1-8 week holds, long only).",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"swing {__version__}")
    p.add_argument("-c", "--config", type=Path, default=None, help="path to config.toml")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="command", metavar="<command>")

    # -- universe -----------------------------------------------------------
    sp = sub.add_parser("universe", help="inspect or refresh the tradable universe")
    sp.add_argument("--refresh", action="store_true", help="re-apply liquidity filters")
    sp.add_argument("--fetch", action="store_true", help="re-download index constituents (network)")
    sp.add_argument("--show", type=int, default=20, help="how many symbols to print")

    # -- data ---------------------------------------------------------------
    sp = sub.add_parser("data", help="manage the local price cache")
    sp.add_argument("--backfill", action="store_true", help="download full history")
    sp.add_argument("--update", action="store_true", help="fetch only missing recent bars")
    sp.add_argument("--symbols", nargs="*", help="limit to these symbols")
    sp.add_argument("--status", action="store_true", help="print cache coverage")
    sp.add_argument("--clear-absent", nargs="*", metavar="SYM", default=None,
                    help="forget symbols marked absent (all of them if none named)")

    # -- backtest -----------------------------------------------------------
    sp = sub.add_parser("backtest", help="run the backtester and write a report")
    sp.add_argument("--walk-forward", action="store_true", help="anchored walk-forward (headline)")
    sp.add_argument("--full", action="store_true", help="single full-period run")
    sp.add_argument("--etf-only", action="store_true", help="ETF universe (survivorship-free)")
    sp.add_argument("--ablations", action="store_true", help="component on/off comparison")
    sp.add_argument("--sensitivity", action="store_true", help="+/-25%% parameter sweep")
    sp.add_argument("--start", default=None)
    sp.add_argument("--end", default=None)
    sp.add_argument("--tag", default=None, help="label for the report directory")

    # -- scan ---------------------------------------------------------------
    sp = sub.add_parser("scan", help="nightly pick generation + alerts")
    sp.add_argument("--dry-run", action="store_true", help="write the sheet, send nothing")
    sp.add_argument("--force", action="store_true", help="bypass the backtest gate (unsafe)")
    sp.add_argument("--date", default=None, help="as-of date, YYYY-MM-DD (default: latest bar)")
    sp.add_argument("--no-refresh", action="store_true", help="use the cache as-is")

    # -- confirm ------------------------------------------------------------
    sp = sub.add_parser("confirm", help="pre-open re-quote of the latest pick sheet")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--sheet", type=Path, default=None, help="path to a specific pick sheet")

    # -- execute ------------------------------------------------------------
    sp = sub.add_parser("execute", help="v2: place drafted orders through Schwab")
    sp.add_argument("--live", action="store_true", help="actually transmit (requires config too)")
    sp.add_argument("--sheet", type=Path, default=None)
    sp.add_argument("--yes", action="store_true", help="skip per-order confirmation prompts")

    # -- auth ---------------------------------------------------------------
    sp = sub.add_parser("auth", help="Schwab OAuth login / token status")
    sp.add_argument("--check", action="store_true", help="verify token, print account + a quote")
    sp.add_argument("--force", action="store_true", help="discard the existing token and re-login")

    # -- doctor -------------------------------------------------------------
    sp = sub.add_parser("doctor", help="check this install can actually do its job")
    sp.add_argument("--offline", action="store_true",
                    help="skip the network probes")

    # -- notify-test --------------------------------------------------------
    sub.add_parser("notify-test", help="send a test message down every alert channel")

    # -- schedule -----------------------------------------------------------
    sp = sub.add_parser("schedule", help="install/remove the launchd jobs (macOS)")
    sp.add_argument("action", choices=["install", "uninstall", "status", "print"])

    # -- kill ---------------------------------------------------------------
    sp = sub.add_parser("kill", help="engage/release the execution kill switch")
    sp.add_argument("--release", action="store_true", help="remove the kill file")

    # -- positions ----------------------------------------------------------
    sub.add_parser("positions", help="show open positions from the journal (and broker)")

    # -- journal ------------------------------------------------------------
    sp = sub.add_parser("journal", help="record and inspect trades in the trade journal")
    jsub = sp.add_subparsers(dest="journal_command", metavar="<action>", required=True)

    jp = jsub.add_parser("add", help="record a manual entry (buy)")
    jp.add_argument("symbol", metavar="SYMBOL")
    jp.add_argument("shares", metavar="SHARES")
    jp.add_argument("price", metavar="PRICE")
    jp.add_argument("stop", metavar="STOP")
    jp.add_argument("--trail", metavar="OFFSET", default=0.0, help="trailing-stop offset")
    jp.add_argument("--order-id", metavar="ID", default=None, help="broker order id")
    jp.add_argument("--note", metavar="TEXT", default="", help="free-text note")
    jp.add_argument(
        "--force", action="store_true",
        help="allow an add that lowers the stop of a position you already hold",
    )

    jp = jsub.add_parser("exit", help="record a full or partial exit (sell)")
    jp.add_argument("symbol", metavar="SYMBOL")
    jp.add_argument("shares", metavar="SHARES")
    jp.add_argument("price", metavar="PRICE")
    jp.add_argument("--reason", metavar="TEXT", default="", help="why you sold")

    jp = jsub.add_parser("stop", help="move a stop (up, unless --force)")
    jp.add_argument("symbol", metavar="SYMBOL")
    jp.add_argument("new_stop", metavar="NEW_STOP")
    jp.add_argument("--force", action="store_true", help="allow lowering the stop (typo fix)")

    jp = jsub.add_parser("show", help="human-readable tail of the event log")
    jp.add_argument("--limit", metavar="N", default=20, help="how many events to print")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(verbose=args.verbose)

    if not args.command:
        parser.print_help()
        return 0

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        log.error(str(exc))
        return 2

    try:
        return _dispatch(args.command, args, cfg)
    except KeyboardInterrupt:
        log.warning("interrupted")
        return 130
    except ConfigError as exc:
        log.error(str(exc))
        return 2


def _dispatch(command: str, args: argparse.Namespace, cfg) -> int:
    if command == "universe":
        from .commands import cmd_universe

        return cmd_universe(args, cfg)
    if command == "data":
        from .commands import cmd_data

        return cmd_data(args, cfg)
    if command == "backtest":
        from .commands import cmd_backtest

        return cmd_backtest(args, cfg)
    if command == "scan":
        from .commands import cmd_scan

        return cmd_scan(args, cfg)
    if command == "confirm":
        from .commands import cmd_confirm

        return cmd_confirm(args, cfg)
    if command == "execute":
        from .commands import cmd_execute

        return cmd_execute(args, cfg)
    if command == "auth":
        from .commands import cmd_auth

        return cmd_auth(args, cfg)
    if command == "doctor":
        from .commands import cmd_doctor

        return cmd_doctor(args, cfg)
    if command == "notify-test":
        from .commands import cmd_notify_test

        return cmd_notify_test(args, cfg)
    if command == "schedule":
        from .commands import cmd_schedule

        return cmd_schedule(args, cfg)
    if command == "kill":
        from .commands import cmd_kill

        return cmd_kill(args, cfg)
    if command == "positions":
        from .commands import cmd_positions

        return cmd_positions(args, cfg)
    if command == "journal":
        from .commands import cmd_journal

        return cmd_journal(args, cfg)
    log.error("unknown command: %s", command)
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
