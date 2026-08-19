"""FROZEN CONTRACT 2 — the ``swing`` command line.

This module is a thin shell and stays that way. Every command does three
things: load configuration, lazily import the module that owns the work, and
call one documented entry point. Nothing here knows how a scan or a backtest
actually works.

The lazy imports are not an optimisation, they are the integration seam: the
package is built work-package by work-package, so ``swing --help`` must keep
working while half the modules do not exist yet, and invoking a command that is
not implemented must print one clear sentence rather than a traceback.

Exit codes, because launchd only ever sees the number: ``0`` success, ``1``
"the work ran but something it did failed" (a dead notification channel in
``notify-test``), ``2`` "swing refused to do the work" — a bad configuration, a
module that is not there, or a ``ScanError``. Scan and confirm ask the pipeline
for ``strict_delivery``, so a night where every channel failed is a refusal
rather than a silent success (audit BUG-021).
"""

from __future__ import annotations

import datetime as _dt
import logging
import warnings
from enum import StrEnum
from importlib import import_module
from pathlib import Path
from typing import Annotated, Any

import typer

from swing import __version__

__all__ = ["app", "main"]

log = logging.getLogger(__name__)

app = typer.Typer(
    name="swing",
    help=(
        "Evidence-gated swing-trade picks. No indicator predicts stocks; every pick this tool "
        "emits is gated behind a walk-forward backtest that passed with realistic costs."
    ),
    no_args_is_help=True,
    add_completion=False,
)


# --------------------------------------------------------------------------
# shared plumbing
# --------------------------------------------------------------------------


class _State:
    """Whatever the top-level callback captured, for the commands to use."""

    def __init__(self) -> None:
        self.config_path: Path | None = None
        self.verbose: bool = False


def _state(ctx: typer.Context) -> _State:
    if not isinstance(ctx.obj, _State):
        ctx.obj = _State()
    return ctx.obj


def _echo_error(message: str) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)


def _load_cfg(ctx: typer.Context) -> Any:
    """Load configuration, turning any config problem into a clean message."""
    from swing.config import ConfigError, load_config

    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", UserWarning)
            cfg = load_config(_state(ctx).config_path)
        for w in caught:
            typer.secho(f"Warning: {w.message}", fg=typer.colors.YELLOW, err=True)
        return cfg
    except ConfigError as exc:
        _echo_error(f"Configuration problem: {exc}")
        raise typer.Exit(code=2) from exc


def _entry(module: str, func: str, command: str) -> Any:
    """Import ``module`` lazily and return ``func`` from it.

    When the module has not been written yet (or does not export the entry
    point), this exits with a single explanatory line instead of a traceback.
    """
    try:
        mod = import_module(module)
    except ImportError as exc:
        _echo_error(
            f"`swing {command}` is not available yet: the module {module} could not be "
            f"imported ({exc}). That part of the system has not been implemented in this "
            f"checkout."
        )
        raise typer.Exit(code=2) from exc
    try:
        return getattr(mod, func)
    except AttributeError as exc:
        _echo_error(
            f"`swing {command}` is not available yet: {module} exists but does not define "
            f"{func}(). That part of the system has not been implemented in this checkout."
        )
        raise typer.Exit(code=2) from exc


class _Unreachable(Exception):
    """A placeholder exception class that nothing ever raises."""


def _scan_error() -> type[Exception]:
    """The pipeline's own refusal exception, imported at the moment it is needed.

    Imported lazily and by name for the same reason the entry points are: the
    CLI must keep working when ``swing.alerts.pipeline`` is not in the
    checkout. When it genuinely is not, nothing can raise ``ScanError`` either,
    so an unmatchable placeholder is exactly right.
    """
    try:
        from swing.alerts.pipeline import ScanError
    except (ImportError, AttributeError):  # pragma: no cover - pipeline always ships
        return _Unreachable
    return ScanError


def _refuse(exc: Exception) -> typer.Exit:
    """Print a refusal as the sentence it already is, and exit 2 (audit BUG-021).

    ``ScanError`` messages are written to be read by a human at a terminal.
    Letting one escape printed a traceback around the sentence and — worse for
    a system whose whole value is the notification — left the exit code at 0,
    so ``launchctl`` reported the failed nightly run as a success.
    """
    _echo_error(str(exc))
    return typer.Exit(code=2)


def _parse_date(value: str | None, option: str) -> _dt.date | None:
    """Parse a YYYY-MM-DD option value, or explain why it could not be parsed."""
    if value is None:
        return None
    try:
        return _dt.date.fromisoformat(value.strip())
    except ValueError as exc:
        _echo_error(f"{option} must be a date written as YYYY-MM-DD, but it is {value!r}.")
        raise typer.Exit(code=2) from exc


class UniverseChoice(StrEnum):
    """Which slice of the universe a backtest should run over."""

    full = "full"
    etf = "etf"
    stocks = "stocks"


class ScheduleAction(StrEnum):
    """What to do with the launchd schedule."""

    install = "install"
    uninstall = "uninstall"
    status = "status"


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"swing {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            "-c",
            metavar="PATH",
            help="Configuration file to use. Default: ./config.toml, then ~/.swing/config.toml.",
        ),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Log what is happening at DEBUG level."),
    ] = False,
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version."),
    ] = False,
) -> None:
    """Swing-trade picks that have to earn their way past a backtest."""
    state = _state(ctx)
    state.config_path = config
    state.verbose = verbose
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


@app.command()
def scan(
    ctx: typer.Context,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Build the report but send no notifications.")
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Emit picks even when the backtest gate has NOT passed."),
    ] = False,
    asof: Annotated[
        str | None,
        typer.Option("--asof", metavar="YYYY-MM-DD", help="Pretend today is this date."),
    ] = None,
) -> None:
    """Run the nightly scan and write a dated report directory."""
    cfg = _load_cfg(ctx)
    day = _parse_date(asof, "--asof")
    run_scan = _entry("swing.alerts.pipeline", "run_scan", "scan")
    try:
        path = run_scan(cfg, dry_run=dry_run, force=force, asof=day, strict_delivery=True)
    except _scan_error() as exc:
        raise _refuse(exc) from exc
    typer.echo(f"Scan report: {path}")


@app.command()
def confirm(
    ctx: typer.Context,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Re-check picks but send no notifications.")
    ] = False,
) -> None:
    """Re-check last night's picks against this morning's prices."""
    cfg = _load_cfg(ctx)
    run_confirm = _entry("swing.alerts.pipeline", "run_confirm", "confirm")
    try:
        path = run_confirm(cfg, dry_run=dry_run, strict_delivery=True)
    except _scan_error() as exc:
        raise _refuse(exc) from exc
    typer.echo(f"Confirmation report: {path}")


@app.command()
def backtest(
    ctx: typer.Context,
    universe: Annotated[
        UniverseChoice, typer.Option("--universe", help="Which slice of the universe to test.")
    ] = UniverseChoice.full,
    start: Annotated[
        str | None, typer.Option("--start", metavar="YYYY-MM-DD", help="Override the start date.")
    ] = None,
    end: Annotated[
        str | None, typer.Option("--end", metavar="YYYY-MM-DD", help="Override the end date.")
    ] = None,
    walkforward: Annotated[
        bool,
        typer.Option(
            "--walkforward/--no-walkforward",
            help="Walk-forward (out-of-sample) run. Only these results can pass the gate.",
        ),
    ] = True,
    label: Annotated[
        str | None, typer.Option("--label", help="Name this run; used as the report folder name.")
    ] = None,
) -> None:
    """Run a backtest and write a report the gate can read."""
    cfg = _load_cfg(ctx)
    start_date = _parse_date(start, "--start")
    end_date = _parse_date(end, "--end")
    run_backtest = _entry("swing.backtest.runner", "run_backtest", "backtest")
    path = run_backtest(
        cfg,
        universe=universe.value,
        start=start_date,
        end=end_date,
        walkforward=walkforward,
        label=label,
    )
    typer.echo(f"Backtest report: {path}")


@app.command()
def report(ctx: typer.Context) -> None:
    """Print a summary of the most recent backtest."""
    cfg = _load_cfg(ctx)
    print_latest = _entry("swing.backtest.report", "print_latest", "report")
    print_latest(cfg)


@app.command()
def auth(
    ctx: typer.Context,
    check: Annotated[
        bool, typer.Option("--check", help="Only report token status; do not start a login flow.")
    ] = False,
) -> None:
    """Log in to Schwab, or check the health of the stored token."""
    cfg = _load_cfg(ctx)
    if check:
        _entry("swing.broker.auth", "check", "auth")(cfg)
    else:
        _entry("swing.broker.auth", "login", "auth")(cfg)


@app.command("notify-test")
def notify_test(ctx: typer.Context) -> None:
    """Send a test message through every configured alert channel."""
    cfg = _load_cfg(ctx)
    run = _entry("swing.alerts.channels", "notify_test", "notify-test")
    results = run(cfg)
    if not results:
        typer.echo("No alert channels are configured; nothing was sent.")
        return
    for channel, ok in sorted(results.items()):
        typer.echo(f"{'ok  ' if ok else 'FAIL'} {channel}")
    if not all(results.values()):
        raise typer.Exit(code=1)


@app.command()
def schedule(
    ctx: typer.Context,
    action: Annotated[
        ScheduleAction, typer.Argument(help="install, uninstall or status.")
    ] = ScheduleAction.status,
) -> None:
    """Install, remove or inspect the launchd timers for scan and confirm."""
    cfg = _load_cfg(ctx)
    _entry("swing.scheduler.launchd", action.value, f"schedule {action.value}")(cfg)


@app.command()
def execute(
    ctx: typer.Context,
    live: Annotated[
        bool,
        typer.Option("--live", help="Actually send orders. Also requires execution.enabled=true."),
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip the per-order confirmation prompt.")
    ] = False,
) -> None:
    """Place today's confirmed orders. Dry-run unless --live is given."""
    cfg = _load_cfg(ctx)
    run_execute = _entry("swing.broker.executor", "run_execute", "execute")
    run_execute(cfg, live=live, assume_yes=yes)


@app.command()
def positions(ctx: typer.Context) -> None:
    """Show open positions as the broker and the local journal each see them."""
    cfg = _load_cfg(ctx)
    _entry("swing.broker.executor", "print_positions", "positions")(cfg)


@app.command()
def kill(
    ctx: typer.Context,
    off: Annotated[
        bool, typer.Option("--off", help="Release the kill switch instead of engaging it.")
    ] = False,
) -> None:
    """Engage (or with --off, release) the kill switch and cancel open orders.

    The kill switch is a file, and switching it is handled here rather than in
    the broker layer, so it keeps working even when the broker module is
    unavailable. Order cancellation is attempted afterwards, best effort.
    """
    cfg = _load_cfg(ctx)
    from swing import state as state_mod

    if off:
        state_mod.clear_kill(cfg)
        typer.secho(
            f"Kill switch RELEASED ({state_mod.kill_path(cfg)} removed). Execution may resume.",
            fg=typer.colors.GREEN,
        )
    else:
        path = state_mod.engage_kill(cfg, reason="engaged via `swing kill`")
        typer.secho(
            f"Kill switch ENGAGED ({path}). No orders will be sent until `swing kill --off`.",
            fg=typer.colors.RED,
        )

    try:
        broker_kill = import_module("swing.broker.executor").kill
    except (ImportError, AttributeError) as exc:
        typer.secho(
            f"Note: could not reach the broker layer to cancel working orders ({exc}). "
            f"The kill switch itself is set — cancel anything open in the Schwab app.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        return
    broker_kill(cfg, off=off)


@app.command()
def universe(
    ctx: typer.Context,
    list_symbols: Annotated[
        bool, typer.Option("--list", help="Print every symbol instead of just the counts.")
    ] = False,
) -> None:
    """Show the tradable universe this configuration selects."""
    cfg = _load_cfg(ctx)
    from swing import universe as universe_mod

    instruments = universe_mod.load(cfg)
    by_source: dict[str, int] = {}
    for inst in instruments:
        by_source[inst.source] = by_source.get(inst.source, 0) + 1
    for source, count in sorted(by_source.items()):
        typer.echo(f"{source:>6}: {count}")
    typer.echo(f"{'total':>6}: {len(instruments)}")
    if list_symbols:
        for inst in instruments:
            typer.echo(f"{inst.symbol}\t{inst.kind}\t{inst.source}\t{inst.name}")


if __name__ == "__main__":  # pragma: no cover
    app()
