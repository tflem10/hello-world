"""Sub-command implementations (thin wiring; real logic lives in modules)."""

from __future__ import annotations

import argparse

from .config import Config
from .logging_setup import get_logger

log = get_logger("swing.commands")


def cmd_universe(args: argparse.Namespace, cfg: Config) -> int:
    from .data.universe import build_universe, describe_universe, fetch_constituents

    if args.fetch:
        written = fetch_constituents("all")
        if not written:
            log.error("no constituent files were refreshed; seed files left untouched")
            return 1
        for key, count in sorted(written.items()):
            print(f"refreshed {key}: {count} symbols")

    uni = build_universe(cfg, apply_liquidity_filter=args.refresh)
    print(describe_universe(uni, limit=args.show))
    return 0


def cmd_data(args: argparse.Namespace, cfg: Config) -> int:
    from .data.pipeline import backfill, cache_status, update

    if args.status or not (args.backfill or args.update):
        print(cache_status(cfg))
        return 0
    if args.backfill:
        backfill(cfg, symbols=args.symbols)
    if args.update:
        update(cfg, symbols=args.symbols)
    return 0


def cmd_backtest(args: argparse.Namespace, cfg: Config) -> int:
    from .backtest.runner import run_backtest_command

    return run_backtest_command(args, cfg)


def cmd_scan(args: argparse.Namespace, cfg: Config) -> int:
    from .scan import run_scan

    return run_scan(cfg, dry_run=args.dry_run, force=args.force, as_of=args.date,
                    refresh=not args.no_refresh)


def cmd_confirm(args: argparse.Namespace, cfg: Config) -> int:
    from .confirm import run_confirm

    return run_confirm(cfg, dry_run=args.dry_run, sheet_path=args.sheet)


def cmd_execute(args: argparse.Namespace, cfg: Config) -> int:
    from .execution.executor import run_execute

    return run_execute(cfg, live=args.live, sheet_path=args.sheet, assume_yes=args.yes)


def cmd_auth(args: argparse.Namespace, cfg: Config) -> int:
    from .auth import run_auth

    return run_auth(cfg, check=args.check, force=args.force)


def cmd_notify_test(args: argparse.Namespace, cfg: Config) -> int:
    from .alerts.dispatch import notify_test

    return notify_test(cfg)


def cmd_schedule(args: argparse.Namespace, cfg: Config) -> int:
    from .schedule import run_schedule

    return run_schedule(cfg, action=args.action)


def cmd_kill(args: argparse.Namespace, cfg: Config) -> int:
    from .execution.guardrails import release_kill_switch, set_kill_switch

    if args.release:
        return release_kill_switch(cfg)
    return set_kill_switch(cfg)


def cmd_positions(args: argparse.Namespace, cfg: Config) -> int:
    from .execution.journal import print_positions

    return print_positions(cfg)


def cmd_journal(args: argparse.Namespace, cfg: Config) -> int:
    from .execution.journal import journal_add, journal_exit, journal_show, journal_stop

    action = getattr(args, "journal_command", None)
    if action == "add":
        return journal_add(
            cfg, args.symbol, args.shares, args.price, args.stop,
            trail=args.trail, order_id=args.order_id, note=args.note,
        )
    if action == "exit":
        return journal_exit(cfg, args.symbol, args.shares, args.price, reason=args.reason)
    if action == "stop":
        return journal_stop(cfg, args.symbol, args.new_stop, force=args.force)
    if action == "show":
        return journal_show(cfg, limit=args.limit)
    log.error("unknown journal action: %s", action)
    return 2
