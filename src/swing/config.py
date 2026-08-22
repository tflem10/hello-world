"""FROZEN CONTRACT 1 — configuration for the whole system.

Every knob the system exposes lives here as a frozen dataclass with a sane
default, so a brand new user can run ``swing scan --dry-run`` without writing a
single line of TOML.  Values are validated at construction time and every
failure is reported as a plain-English sentence that names the setting, says
what is allowed, shows what was given, and points at the file to fix.

Search order used by :func:`load_config`:

1. an explicit ``--config PATH``
2. ``./config.toml`` (project-local)
3. ``~/.swing/config.toml`` (per-user)
4. the committed ``config.example.toml`` defaults, with a loud warning

A relative ``paths.reports_dir`` is resolved against the directory of the file
it was read from (audit BUG-022), so ``swing confirm`` run from ``~`` finds the
same reports ``swing scan`` wrote from the project directory.

Ranges are not only about absurd values. Several knobs have a *cliff* — a
setting that validates cleanly and then silently disables the strategy, which
looks exactly like a quiet market. Those are capped against the history the
system actually fetches; see :data:`MAX_LOOKBACK_BARS` (audit BUG-029/032).
"""

from __future__ import annotations

import datetime as _dt
import logging
import math
import tomllib
import warnings
from dataclasses import MISSING, dataclass, field, fields, replace
from pathlib import Path
from typing import Any, get_args, get_origin

from swing.universe import UNKNOWN_EXCLUDE, UNKNOWN_POLICIES

__all__ = [
    "MAX_LOOKBACK_BARS",
    "MAX_MOMENTUM_LOOKBACK_BARS",
    "MAX_TUNING_COMBINATIONS",
    "MEMBERSHIP_MODES",
    "MEMBERSHIP_OFF",
    "MEMBERSHIP_POINT_IN_TIME",
    "TUNABLE_PARAMS",
    "AccountCfg",
    "AlertsCfg",
    "BacktestCfg",
    "Config",
    "ConfigError",
    "DataCfg",
    "ExecutionCfg",
    "GatesCfg",
    "PathsCfg",
    "RegimeCfg",
    "ScheduleCfg",
    "SchwabCfg",
    "StrategyCfg",
    "UniverseCfg",
    "config_search_paths",
    "load_config",
]

log = logging.getLogger(__name__)

EXAMPLE_FILENAME = "config.example.toml"
CONFIG_FILENAME = "config.toml"

#: The longest lookback any single indicator window may ask for.
#:
#: The scan fetches ``asof - 600 calendar days`` (``SCAN_LOOKBACK_DAYS``), which
#: is roughly 413 trading bars. A window longer than that can never produce a
#: value, so the whole scan comes back empty with no error — indistinguishable
#: from a quiet market (audit BUG-032). 380 leaves ~30 bars of margin for
#: holidays and a short warm-up.
MAX_LOOKBACK_BARS = 380

#: The longest *momentum* lookback, which is stricter: ranking additionally
#: requires ``MIN_HISTORY_ROWS`` (260) rows of history, and the 126-day return
#: is measured ``mom_skip_days`` before the last bar. ``mom_skip_days + 126``
#: past 250 leaves no room inside that floor and ranks nothing, ever.
MAX_MOMENTUM_LOOKBACK_BARS = 250

#: The momentum window the ranking blend reaches furthest back for.
_MOMENTUM_LOOKBACK_BARS = 126

#: The only parameters a walk-forward tuning grid may name.
#:
#: The probability that a backtest is overfitted rises with the number of
#: trials, so this list is short on purpose and is the thing a research change
#: has to argue past. It is mirrored by
#: :data:`swing.backtest.walkforward.TUNING_GRID`, whose keys must stay
#: identical to these (a test pins the pair together).
TUNABLE_PARAMS: tuple[str, ...] = (
    "atr_stop_mult",
    "chandelier_mult",
    "donchian_window",
    "volume_mult",
)

#: Today's index membership applied to all of history — what every report
#: written so far used, and still the default so those reports stay
#: reproducible. It is a *look-ahead*: a company is traded during years when it
#: was not in the index, and index inclusion is itself an outcome of past
#: growth (``docs/backtest-methodology.md`` §7).
MEMBERSHIP_OFF = "off"

#: A symbol is tradable only on days a membership file says it was in an index.
MEMBERSHIP_POINT_IN_TIME = "point_in_time"

#: How ``[backtest] membership`` may decide who was in the universe when.
MEMBERSHIP_MODES: tuple[str, ...] = (MEMBERSHIP_OFF, MEMBERSHIP_POINT_IN_TIME)

#: The most parameter combinations one ``[backtest.tuning_grid]`` may ask for.
#:
#: Every combination is simulated over every symbol in every walk-forward fold,
#: so the bill is combinations x folds x symbols; the default grid spends 81 of
#: this budget. The cap is a cost ceiling first and an honesty ceiling second —
#: a grid that searches harder buys in-sample fit with out-of-sample credibility.
MAX_TUNING_COMBINATIONS = 512

#: What each tunable candidate must be, before its range is checked. Mirrors the
#: annotation the matching :class:`StrategyCfg` field carries.
_TUNABLE_TYPES: dict[str, type] = {
    "atr_stop_mult": float,
    "chandelier_mult": float,
    "donchian_window": int,
    "volume_mult": float,
}


class ConfigError(ValueError):
    """Raised when a configuration value is missing, unknown or out of range.

    The message is always a complete, plain-English sentence intended to be
    printed straight to a user's terminal — never a stack trace.
    """


# --------------------------------------------------------------------------
# small validation helpers — each returns None or raises ConfigError
# --------------------------------------------------------------------------


def _where(section: str, key: str) -> str:
    return f"Fix the `{key}` setting in the [{section}] section of your config.toml."


def _require(condition: bool, message: str, section: str, key: str) -> None:
    if not condition:
        raise ConfigError(f"{message} {_where(section, key)}")


def _positive(value: float, section: str, key: str, what: str) -> None:
    _require(
        value > 0,
        f"{section}.{key} ({what}) must be greater than 0, but it is {value}.",
        section,
        key,
    )


def _in_range(
    value: float,
    low: float,
    high: float,
    section: str,
    key: str,
    what: str,
    *,
    low_inclusive: bool = False,
) -> None:
    low_ok = value >= low if low_inclusive else value > low
    if not (low_ok and value <= high):
        bound = "at least" if low_inclusive else "greater than"
        raise ConfigError(
            f"{section}.{key} ({what}) must be {bound} {low} and at most {high}, "
            f"but it is {value}. {_where(section, key)}"
        )


def _at_least(value: float, minimum: float, section: str, key: str, what: str) -> None:
    _require(
        value >= minimum,
        f"{section}.{key} ({what}) must be at least {minimum}, but it is {value}.",
        section,
        key,
    )


def _one_of(value: str, allowed: tuple[str, ...], section: str, key: str) -> None:
    _require(
        value in allowed,
        f"{section}.{key} must be one of {', '.join(allowed)}, but it is {value!r}.",
        section,
        key,
    )


def _hhmm(value: str, section: str, key: str) -> None:
    parts = value.split(":")
    ok = len(parts) == 2 and all(p.isdigit() for p in parts)
    if ok:
        hour, minute = int(parts[0]), int(parts[1])
        ok = 0 <= hour <= 23 and 0 <= minute <= 59
    _require(
        ok,
        f"{section}.{key} must be a 24-hour clock time like '17:30', but it is {value!r}.",
        section,
        key,
    )


def _check_tunable_value(name: str, value: Any, key: str) -> None:
    """Hold one tuning-grid candidate to the constraint ``[strategy]`` puts on it.

    The walk-forward writes the value it picks straight into
    :class:`StrategyCfg`, so a candidate the strategy would refuse is not a
    smaller mistake for being offered rather than configured — it is the same
    mistake, discovered several hundred simulations later. The wording is kept
    identical to the ``[strategy]`` check on purpose; a test asserts the two
    accept and refuse exactly the same values.
    """
    if name == "atr_stop_mult":
        _positive(value, "backtest", key, "initial stop distance in ATRs")
    elif name == "chandelier_mult":
        _positive(value, "backtest", key, "trailing stop distance in ATRs")
    elif name == "volume_mult":
        _positive(value, "backtest", key, "breakout volume vs its average, a multiple")
    else:  # donchian_window
        _at_least(value, 2, "backtest", key, "a lookback window in bars")
        _require(
            value <= MAX_LOOKBACK_BARS,
            f"backtest.{key} (a lookback window in bars) must be at most {MAX_LOOKBACK_BARS}, "
            f"but it is {value}. Longer than that and the window never fills from the history "
            f"swing fetches, so every scan would come back empty.",
            "backtest",
            key,
        )


# --------------------------------------------------------------------------
# type coercion — TOML gives us ints/strings where we want floats/Paths/dates
# --------------------------------------------------------------------------


def _coerce(obj: Any) -> None:
    """Normalise field types in place on a frozen dataclass instance.

    TOML (and hand-written test fixtures) will hand us ``100`` for a float
    field, a ``str`` for a ``Path`` field, and a ``list`` for a ``tuple`` field.
    Rather than making every caller be pedantic we normalise once, here.
    """
    for f in fields(obj):
        value = getattr(obj, f.name)
        ann = f.type if not isinstance(f.type, str) else _ANNOTATIONS[type(obj)][f.name]
        new = _coerce_value(value, ann, type(obj).__name__, f.name)
        if new is not value:
            object.__setattr__(obj, f.name, new)


def _optional_inner(ann: Any) -> Any:
    """Return ``X`` for ``X | None`` annotations, else the annotation itself."""
    args = [a for a in get_args(ann) if a is not type(None)]
    if get_origin(ann) is not None and len(args) == 1 and len(get_args(ann)) == 2:
        return args[0]
    return ann


def _type_error(cls_name: str, key: str, value: Any, wanted: str) -> ConfigError:
    """A plain-English "wrong kind of value" sentence naming the setting."""
    section = _SECTION_OF.get(cls_name, cls_name)
    return ConfigError(
        f"{section}.{key} must be {wanted}, but it is {value!r}. {_where(section, key)}"
    )


def _coerce_value(value: Any, ann: Any, cls_name: str, key: str) -> Any:
    if value is None:
        return None
    inner = _optional_inner(ann)
    if inner in (int, float):
        # A quoted number is the most common TOML mistake there is, and a bare
        # `true` for a number the second; both used to reach the comparison
        # operators and die as a raw TypeError (audit BUG-033).
        if isinstance(value, bool):
            raise _type_error(cls_name, key, value, "a number, not true or false")
        if isinstance(value, str):
            raise _type_error(
                cls_name, key, value, "a number written without quotes, such as 50 or 2.5"
            )
        if not isinstance(value, int | float):
            raise _type_error(cls_name, key, value, "a number")
    if inner is float and isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    if inner is Path and isinstance(value, str):
        return Path(value).expanduser()
    if inner is Path and isinstance(value, Path):
        return value.expanduser()
    if inner is _dt.date:
        if isinstance(value, _dt.datetime):
            return value.date()
        if isinstance(value, str):
            try:
                return _dt.date.fromisoformat(value)
            except ValueError as exc:
                raise ConfigError(
                    f"{_SECTION_OF.get(cls_name, cls_name)}.{key} must be a date written as "
                    f"YYYY-MM-DD, but it is {value!r}."
                ) from exc
        return value
    if get_origin(inner) is tuple and isinstance(value, list | tuple):
        return tuple(str(v).strip().upper() for v in value)
    if inner is str and not isinstance(value, str):
        raise _type_error(cls_name, key, value, "text in quotes")
    if inner is bool and not isinstance(value, bool):
        raise ConfigError(
            f"{_SECTION_OF.get(cls_name, cls_name)}.{key} must be true or false, "
            f"but it is {value!r}."
        )
    if inner is int and isinstance(value, float) and not float(value).is_integer():
        raise ConfigError(
            f"{_SECTION_OF.get(cls_name, cls_name)}.{key} must be a whole number, "
            f"but it is {value!r}."
        )
    if inner is int and isinstance(value, float):
        return int(value)
    return value


# --------------------------------------------------------------------------
# sections
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AccountCfg:
    """How much money is at stake and how hard it may be risked."""

    equity: float = 100.0
    risk_pct: float = 2.5
    max_positions: int = 4
    max_position_pct: float = 25.0

    def __post_init__(self) -> None:
        _coerce(self)
        _positive(self.equity, "account", "equity", "your account size in dollars")
        _in_range(
            self.risk_pct, 0.0, 10.0, "account", "risk_pct", "percent of equity risked per trade"
        )
        _at_least(
            self.max_positions, 1, "account", "max_positions", "how many positions may be open"
        )
        _in_range(
            self.max_position_pct,
            0.0,
            100.0,
            "account",
            "max_position_pct",
            "percent of equity in any single position",
        )


@dataclass(frozen=True)
class UniverseCfg:
    """Which lists of tradable symbols to scan."""

    sp500: bool = True
    sp400: bool = True
    sp600: bool = True
    etfs: bool = True
    extra_symbols: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _coerce(self)
        _require(
            any((self.sp500, self.sp400, self.sp600, self.etfs)) or bool(self.extra_symbols),
            "The universe is empty: every index is switched off and no extra_symbols were "
            "given, so there would be nothing to scan.",
            "universe",
            "sp500",
        )
        for sym in self.extra_symbols:
            _require(
                bool(sym) and sym.replace("-", "").replace(".", "").isalnum(),
                f"universe.extra_symbols contains {sym!r}, which is not a valid ticker.",
                "universe",
                "extra_symbols",
            )


@dataclass(frozen=True)
class DataCfg:
    """Where price history comes from, where it is cached, and how hard to try.

    The network knobs exist because a rate-limited user's only remedy used to
    be editing the source (audit DEBT-013). They are deliberately narrow: they
    tune politeness, not correctness.
    """

    provider: str = "yfinance"
    cache_dir: Path = field(default_factory=lambda: Path.home() / ".swing" / "cache")
    start_date: _dt.date = _dt.date(2010, 1, 1)
    retries: int = 3
    retry_backoff: float = 0.5
    download_batch: int = 200

    def __post_init__(self) -> None:
        _coerce(self)
        _one_of(self.provider, ("yfinance", "schwab"), "data", "provider")
        _require(
            self.start_date >= _dt.date(1970, 1, 1),
            f"data.start_date must be 1970-01-01 or later, but it is {self.start_date}.",
            "data",
            "start_date",
        )
        _in_range(
            self.retries,
            1,
            10,
            "data",
            "retries",
            "how many times a failed download is attempted, including the first try",
            low_inclusive=True,
        )
        _in_range(
            self.retry_backoff,
            0.0,
            10.0,
            "data",
            "retry_backoff",
            "seconds to wait before the first retry, doubling each time",
        )
        _in_range(
            self.download_batch,
            10,
            500,
            "data",
            "download_batch",
            "how many symbols are requested in one download",
            low_inclusive=True,
        )


@dataclass(frozen=True)
class StrategyCfg:
    """The trend/momentum rule set. Every threshold is a research knob."""

    min_price: float = 5.0
    min_dollar_volume: float = 5_000_000.0
    sma_fast: int = 50
    sma_mid: int = 150
    sma_slow: int = 200
    sma_slow_rising_days: int = 21
    min_above_low_mult: float = 1.25
    max_below_high_pct: float = 25.0
    adx_min: float = 20.0
    donchian_window: int = 20
    breakout_proximity_pct: float = 2.0
    volume_mult: float = 1.3
    volume_avg_window: int = 50
    atr_window: int = 14
    atr_stop_mult: float = 2.0
    chandelier_mult: float = 3.0
    time_stop_days: int = 40
    earnings_blackout_days: int = 10
    mom_weight_126: float = 0.6
    mom_weight_63: float = 0.4
    mom_skip_days: int = 5
    rsi2_enabled: bool = False
    fundamentals_filter: bool = True

    def __post_init__(self) -> None:
        _coerce(self)
        _positive(self.min_price, "strategy", "min_price", "cheapest share price to consider")
        _positive(
            self.min_dollar_volume,
            "strategy",
            "min_dollar_volume",
            "minimum average daily dollar volume",
        )
        for key in ("sma_fast", "sma_mid", "sma_slow", "donchian_window", "volume_avg_window"):
            _at_least(getattr(self, key), 2, "strategy", key, "a lookback window in bars")
        _at_least(self.atr_window, 2, "strategy", "atr_window", "the ATR lookback in bars")
        _require(
            self.sma_fast < self.sma_mid < self.sma_slow,
            "strategy.sma_fast, sma_mid and sma_slow must get longer in that order "
            f"(got {self.sma_fast}, {self.sma_mid}, {self.sma_slow}).",
            "strategy",
            "sma_fast",
        )
        _at_least(
            self.sma_slow_rising_days,
            1,
            "strategy",
            "sma_slow_rising_days",
            "how far back the slow SMA must have risen",
        )
        _at_least(
            self.min_above_low_mult,
            1.0,
            "strategy",
            "min_above_low_mult",
            "how far above the 52-week low price must be, as a multiple",
        )
        _in_range(
            self.max_below_high_pct,
            0.0,
            100.0,
            "strategy",
            "max_below_high_pct",
            "how far below the 52-week high price may be, in percent",
        )
        _in_range(
            self.adx_min,
            0.0,
            100.0,
            "strategy",
            "adx_min",
            "minimum ADX trend strength; 0 disables the filter",
            low_inclusive=True,
        )
        _in_range(
            self.breakout_proximity_pct,
            0.0,
            25.0,
            "strategy",
            "breakout_proximity_pct",
            "how close to the breakout level still counts, in percent; 0 requires a strict "
            "breakout, and above 25 the test stops discriminating at all",
            low_inclusive=True,
        )
        _positive(
            self.volume_mult,
            "strategy",
            "volume_mult",
            "breakout volume vs its average, a multiple",
        )
        _positive(self.atr_stop_mult, "strategy", "atr_stop_mult", "initial stop distance in ATRs")
        _positive(
            self.chandelier_mult, "strategy", "chandelier_mult", "trailing stop distance in ATRs"
        )
        _at_least(self.time_stop_days, 1, "strategy", "time_stop_days", "maximum holding period")
        _at_least(
            self.earnings_blackout_days,
            0,
            "strategy",
            "earnings_blackout_days",
            "days around earnings when new entries are blocked",
        )
        for key in ("mom_weight_126", "mom_weight_63"):
            _in_range(
                getattr(self, key),
                0.0,
                1.0,
                "strategy",
                key,
                "a momentum blend weight",
                low_inclusive=True,
            )
        _positive(
            self.mom_weight_126 + self.mom_weight_63,
            "strategy",
            "mom_weight_126",
            "the two momentum weights added together",
        )
        _at_least(
            self.mom_skip_days,
            0,
            "strategy",
            "mom_skip_days",
            "recent days skipped when measuring momentum",
        )
        self._check_lookbacks()

    def _check_lookbacks(self) -> None:
        """Refuse windows longer than the history the system ever holds (BUG-032).

        Each of these validates fine on its own and then produces a scan that
        is permanently, silently empty — the failure mode that looks exactly
        like a quiet market. The bound is :data:`MAX_LOOKBACK_BARS`, derived
        once from the fetch window rather than restated per knob.
        """
        for key in ("donchian_window", "volume_avg_window", "atr_window"):
            value = getattr(self, key)
            _require(
                value <= MAX_LOOKBACK_BARS,
                f"strategy.{key} (a lookback window in bars) must be at most "
                f"{MAX_LOOKBACK_BARS}, but it is {value}. Longer than that and the window never "
                f"fills from the history swing fetches, so every scan would come back empty.",
                "strategy",
                key,
            )
        trend_bars = self.sma_slow + self.sma_slow_rising_days
        _require(
            trend_bars <= MAX_LOOKBACK_BARS,
            f"strategy.sma_slow ({self.sma_slow}) plus strategy.sma_slow_rising_days "
            f"({self.sma_slow_rising_days}) needs {trend_bars} bars of history, which is more "
            f"than the {MAX_LOOKBACK_BARS} swing fetches, so the trend test could never pass "
            f"and every scan would come back empty.",
            "strategy",
            "sma_slow",
        )
        momentum_bars = self.mom_skip_days + _MOMENTUM_LOOKBACK_BARS
        _require(
            momentum_bars <= MAX_MOMENTUM_LOOKBACK_BARS,
            f"strategy.mom_skip_days ({self.mom_skip_days}) plus the {_MOMENTUM_LOOKBACK_BARS}-day "
            f"momentum window needs {momentum_bars} bars, which is more than the "
            f"{MAX_MOMENTUM_LOOKBACK_BARS} the ranking has room for, so nothing would ever be "
            f"ranked.",
            "strategy",
            "mom_skip_days",
        )


@dataclass(frozen=True)
class RegimeCfg:
    """Market-wide filter: only take new entries when the market is healthy."""

    enabled: bool = True
    symbol: str = "SPY"
    sma_window: int = 200

    def __post_init__(self) -> None:
        _coerce(self)
        _require(
            bool(self.symbol.strip()),
            "regime.symbol must name a ticker such as 'SPY', but it is empty.",
            "regime",
            "symbol",
        )
        _at_least(self.sma_window, 2, "regime", "sma_window", "the regime SMA lookback in bars")


@dataclass(frozen=True)
class BacktestCfg:
    """Backtest window, trading costs and the walk-forward split."""

    start: _dt.date = _dt.date(2010, 1, 1)
    end: _dt.date | None = None
    slippage_bps: float = 5.0
    spread_atr_frac: float = 0.05
    is_years: int = 3
    oos_years: int = 1
    #: Reference capital the backtest trades. Deliberately separate from
    #: account.equity: the backtest is measuring the STRATEGY, so it needs a
    #: fixed, comparable capital base. Sized on a real $100 account, whole-share
    #: rounding would reject nearly every entry and the run would prove nothing.
    initial_equity: float = 10_000.0
    #: Which universe the backtest trades on each historical day.
    #: :data:`MEMBERSHIP_OFF` (the default) applies *today's* index membership
    #: to all of history, which is what every existing report did and is a
    #: look-ahead worth 30–64% of the nominal member-years depending on how
    #: unstated join dates are read (``docs/backtest-methodology.md`` §7.1).
    #: :data:`MEMBERSHIP_POINT_IN_TIME` makes a symbol tradable only between its
    #: stated join and removal dates. The default stays ``off`` so existing
    #: reports remain reproducible, and so this knob cannot change a number
    #: nobody asked it to change.
    membership: str = MEMBERSHIP_OFF
    #: What point-in-time membership does about a symbol whose join date no
    #: source states — ``"exclude"`` (the default, conservative: it is not
    #: treated as a member) or ``"include"`` (it is a member from the beginning
    #: of the data). Not a detail: 42% of current S&P 600 members have no
    #: stated join date, so ``"include"`` quietly reinstates the bias for them.
    #: Inert while ``membership`` is ``off``.
    membership_unknown: str = UNKNOWN_EXCLUDE
    #: Candidate values the walk-forward may choose between, one list per
    #: parameter, written as a ``[backtest.tuning_grid]`` table. ``None`` — the
    #: default — means the standard grid in
    #: :data:`swing.backtest.walkforward.TUNING_GRID`, so an absent section
    #: reproduces every report written before this knob existed. Naming only
    #: some of :data:`TUNABLE_PARAMS` is allowed and means the rest are not
    #: tuned at all: they keep their ``[strategy]`` value in every fold.
    tuning_grid: dict[str, tuple[Any, ...]] | None = None

    def __post_init__(self) -> None:
        _coerce(self)
        if self.end is not None:
            _require(
                self.start < self.end,
                f"backtest.start ({self.start}) must be earlier than backtest.end ({self.end}).",
                "backtest",
                "start",
            )
        else:
            # With no end date the run goes to the latest available bar, so the
            # only thing that can make the window empty is a start in the
            # future — which used to load quite happily (audit BUG-032).
            today = _dt.date.today()
            _require(
                self.start < today,
                f"backtest.start ({self.start}) must be in the past, but today is {today} and "
                f"backtest.end is not set, so the backtest window would be empty.",
                "backtest",
                "start",
            )
        _at_least(
            self.slippage_bps, 0.0, "backtest", "slippage_bps", "slippage per side in basis points"
        )
        _at_least(
            self.spread_atr_frac,
            0.0,
            "backtest",
            "spread_atr_frac",
            "assumed half-spread as a fraction of ATR",
        )
        _at_least(self.is_years, 1, "backtest", "is_years", "in-sample years per walk-forward fold")
        _at_least(
            self.oos_years, 1, "backtest", "oos_years", "out-of-sample years per walk-forward fold"
        )
        _positive(
            self.initial_equity,
            "backtest",
            "initial_equity",
            "the reference capital the backtest trades with",
        )
        _at_least(
            self.initial_equity,
            100.0,
            "backtest",
            "initial_equity",
            "the reference capital the backtest trades with — below $100 whole-share rounding "
            "rejects almost every entry, so the run would measure nothing",
        )
        _one_of(self.membership, MEMBERSHIP_MODES, "backtest", "membership")
        _require(
            self.membership_unknown in UNKNOWN_POLICIES,
            f"backtest.membership_unknown must be one of {', '.join(UNKNOWN_POLICIES)}, but it "
            f"is {self.membership_unknown!r}. 'exclude' leaves a symbol out on the days no "
            f"source says it was in an index; 'include' treats an unstated join date as "
            f"'a member from the beginning of the data', which reinstates the very look-ahead "
            f"backtest.membership exists to remove.",
            "backtest",
            "membership_unknown",
        )
        self._check_tuning_grid()

    def _check_tuning_grid(self) -> None:
        """Validate ``[backtest.tuning_grid]`` and store it in a canonical order.

        This table decides what the walk-forward tuner is *allowed* to choose,
        which makes a mistake here quieter than most: a misspelled parameter or
        a value the strategy cannot take does not crash a run, it silently runs
        a different experiment and reports it as this one. So the parameter
        list is closed, every candidate faces the same limits ``[strategy]``
        imposes, and the combination count is capped.

        Key order is normalised to :data:`TUNABLE_PARAMS` rather than left as
        written, because the tuner breaks ties on the grid's own ordering: two
        config files listing the same candidates in a different order would
        otherwise be able to select different parameters from identical data.
        """
        grid = self.tuning_grid
        if grid is None:
            return
        if not isinstance(grid, dict):
            raise ConfigError(
                "backtest.tuning_grid must be a table of parameter names and their candidate "
                "values, written as a [backtest.tuning_grid] section holding lines like "
                f"`atr_stop_mult = [1.5, 2.0, 2.5]`, but it is {grid!r}. "
                f"{_where('backtest', 'tuning_grid')}"
            )

        unknown = sorted(set(grid) - set(TUNABLE_PARAMS))
        if unknown:
            raise ConfigError(
                f"backtest.tuning_grid names {', '.join(repr(u) for u in unknown)}, which the "
                f"walk-forward cannot tune. The only tunable parameters are "
                f"{', '.join(TUNABLE_PARAMS)}. {_where('backtest', 'tuning_grid')}"
            )
        if not grid:
            raise ConfigError(
                "backtest.tuning_grid is empty, so the walk-forward would have nothing to choose "
                "between and every fold would simply run the [strategy] values. List candidates "
                f"for at least one of {', '.join(TUNABLE_PARAMS)}, or delete the section "
                f"altogether to use the standard grid. {_where('backtest', 'tuning_grid')}"
            )

        canonical = {
            name: self._check_tuning_candidates(name, grid[name])
            for name in TUNABLE_PARAMS
            if name in grid
        }
        combinations = math.prod(len(values) for values in canonical.values())
        _require(
            combinations <= MAX_TUNING_COMBINATIONS,
            f"backtest.tuning_grid asks for {combinations} parameter combinations, but at most "
            f"{MAX_TUNING_COMBINATIONS} are allowed. Every combination is simulated over every "
            f"symbol in every walk-forward fold, so the cost is combinations x folds x symbols; "
            f"shorten one of the lists.",
            "backtest",
            "tuning_grid",
        )
        object.__setattr__(self, "tuning_grid", canonical)

    @staticmethod
    def _check_tuning_candidates(name: str, raw: Any) -> tuple[Any, ...]:
        """Validate one parameter's candidate list and return it as a tuple."""
        key = f"tuning_grid.{name}"
        if isinstance(raw, str) or not isinstance(raw, list | tuple):
            raise ConfigError(
                f"backtest.{key} must be a list of candidate values written like "
                f"[1.5, 2.0, 2.5], but it is {raw!r}. {_where('backtest', key)}"
            )
        if not raw:
            raise ConfigError(
                f"backtest.{key} is an empty list, so the walk-forward would have no value to "
                f"choose for {name}. Give it at least one candidate, or drop the line to leave "
                f"{name} out of the grid entirely. {_where('backtest', key)}"
            )
        values = tuple(
            _coerce_value(value, _TUNABLE_TYPES[name], "BacktestCfg", key) for value in raw
        )
        for value in values:
            _check_tunable_value(name, value, key)
        repeated = sorted({value for value in values if values.count(value) > 1})
        if repeated:
            raise ConfigError(
                f"backtest.{key} lists {', '.join(str(v) for v in repeated)} more than once. "
                f"A repeated candidate is simulated again for the same answer, and the report's "
                f"candidate count would overstate how wide the search really was. "
                f"{_where('backtest', key)}"
            )
        return values


@dataclass(frozen=True)
class GatesCfg:
    """The bar the out-of-sample backtest must clear before picks are emitted."""

    min_profit_factor: float = 1.3
    max_drawdown_pct: float = 35.0
    min_trades: int = 30

    def __post_init__(self) -> None:
        _coerce(self)
        _positive(
            self.min_profit_factor,
            "gates",
            "min_profit_factor",
            "the smallest acceptable profit factor",
        )
        _in_range(
            self.max_drawdown_pct,
            0.0,
            100.0,
            "gates",
            "max_drawdown_pct",
            "the worst acceptable drawdown, in percent",
        )
        _at_least(
            self.min_trades, 1, "gates", "min_trades", "how many trades make the result meaningful"
        )


@dataclass(frozen=True)
class AlertsCfg:
    """Notification channels. Empty strings simply switch a channel off."""

    ntfy_topic: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    email_to: str = ""
    sms_gateway_address: str = ""
    macos_notify: bool = True

    def __post_init__(self) -> None:
        _coerce(self)
        _in_range(self.smtp_port, 0, 65535, "alerts", "smtp_port", "the SMTP server port")
        if self.email_to and not self.smtp_host:
            raise ConfigError(
                "alerts.email_to is set but alerts.smtp_host is empty, so no email can be sent. "
                "Either fill in smtp_host or clear email_to in the [alerts] section of your "
                "config.toml."
            )


@dataclass(frozen=True)
class SchwabCfg:
    """Schwab developer-app credentials. Never commit these."""

    api_key: str = ""
    app_secret: str = ""
    callback_url: str = "https://127.0.0.1:8182"
    token_path: Path = field(default_factory=lambda: Path.home() / ".swing" / "schwab_token.json")
    account_index: int = 0

    def __post_init__(self) -> None:
        _coerce(self)
        _require(
            self.callback_url.startswith("https://"),
            f"schwab.callback_url must start with https:// (Schwab requires TLS), but it is "
            f"{self.callback_url!r}.",
            "schwab",
            "callback_url",
        )
        _at_least(
            self.account_index,
            0,
            "schwab",
            "account_index",
            "which linked account to trade, counting from 0",
        )


@dataclass(frozen=True)
class ExecutionCfg:
    """Auto-execution guardrails. Everything defaults to OFF on purpose."""

    enabled: bool = False
    autopilot: bool = False
    max_orders_per_day: int = 3
    max_new_exposure_pct: float = 50.0
    max_quote_drift_atr: float = 1.0
    max_quote_drift_pct: float = 3.0

    def __post_init__(self) -> None:
        _coerce(self)
        _at_least(
            self.max_orders_per_day,
            0,
            "execution",
            "max_orders_per_day",
            "how many orders may be sent in one day",
        )
        _in_range(
            self.max_new_exposure_pct,
            0.0,
            100.0,
            "execution",
            "max_new_exposure_pct",
            "percent of equity that may be newly committed in one day",
        )
        _positive(
            self.max_quote_drift_atr,
            "execution",
            "max_quote_drift_atr",
            "how far price may move from the scan, in ATRs",
        )
        _positive(
            self.max_quote_drift_pct,
            "execution",
            "max_quote_drift_pct",
            "how far price may move from the scan, in percent",
        )
        if self.autopilot and not self.enabled:
            raise ConfigError(
                "execution.autopilot is on but execution.enabled is off, so nothing would ever "
                "be sent. Turn on execution.enabled as well, or turn autopilot off, in the "
                "[execution] section of your config.toml."
            )


@dataclass(frozen=True)
class ScheduleCfg:
    """When the nightly scan and the morning confirmation run."""

    scan_time: str = "17:30"
    confirm_time: str = "09:00"
    timezone: str = "America/New_York"

    def __post_init__(self) -> None:
        _coerce(self)
        _hhmm(self.scan_time, "schedule", "scan_time")
        _hhmm(self.confirm_time, "schedule", "confirm_time")
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(self.timezone)
        except Exception as exc:  # noqa: BLE001 - any zoneinfo failure is a config problem
            raise ConfigError(
                f"schedule.timezone must be an IANA timezone name such as 'America/New_York', "
                f"but {self.timezone!r} was not recognised. "
                f"{_where('schedule', 'timezone')}"
            ) from exc


@dataclass(frozen=True)
class PathsCfg:
    """Where reports and local state are written."""

    reports_dir: Path = field(default_factory=lambda: Path("reports"))
    state_dir: Path = field(default_factory=lambda: Path.home() / ".swing")

    def __post_init__(self) -> None:
        _coerce(self)
        for key in ("reports_dir", "state_dir"):
            value = getattr(self, key)
            _require(
                str(value).strip() != "",
                f"paths.{key} must be a directory path, but it is empty.",
                "paths",
                key,
            )


@dataclass(frozen=True)
class Config:
    """The whole configuration. Sections mirror ``config.example.toml`` 1:1."""

    account: AccountCfg = field(default_factory=AccountCfg)
    universe: UniverseCfg = field(default_factory=UniverseCfg)
    data: DataCfg = field(default_factory=DataCfg)
    strategy: StrategyCfg = field(default_factory=StrategyCfg)
    regime: RegimeCfg = field(default_factory=RegimeCfg)
    backtest: BacktestCfg = field(default_factory=BacktestCfg)
    gates: GatesCfg = field(default_factory=GatesCfg)
    alerts: AlertsCfg = field(default_factory=AlertsCfg)
    schwab: SchwabCfg = field(default_factory=SchwabCfg)
    execution: ExecutionCfg = field(default_factory=ExecutionCfg)
    schedule: ScheduleCfg = field(default_factory=ScheduleCfg)
    paths: PathsCfg = field(default_factory=PathsCfg)


_SECTIONS: dict[str, type] = {
    "account": AccountCfg,
    "universe": UniverseCfg,
    "data": DataCfg,
    "strategy": StrategyCfg,
    "regime": RegimeCfg,
    "backtest": BacktestCfg,
    "gates": GatesCfg,
    "alerts": AlertsCfg,
    "schwab": SchwabCfg,
    "execution": ExecutionCfg,
    "schedule": ScheduleCfg,
    "paths": PathsCfg,
}
_SECTION_OF: dict[str, str] = {cls.__name__: name for name, cls in _SECTIONS.items()}

# Resolved once at import time so ``_coerce`` never pays for typing.get_type_hints.
_ANNOTATIONS: dict[type, dict[str, Any]] = {}


def _resolve_annotations() -> None:
    import typing

    namespace = {
        "Path": Path,
        "date": _dt.date,
        "_dt": _dt,
        "Any": Any,
        **{cls.__name__: cls for cls in _SECTIONS.values()},
    }
    for cls in (*_SECTIONS.values(), Config):
        _ANNOTATIONS[cls] = typing.get_type_hints(cls, globalns={**globals(), **namespace})


_resolve_annotations()


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------


def config_search_paths(explicit: Path | None = None) -> list[Path]:
    """Return the config files that :func:`load_config` will look at, in order."""
    paths: list[Path] = []
    if explicit is not None:
        paths.append(Path(explicit).expanduser())
    paths.append(Path.cwd() / CONFIG_FILENAME)
    paths.append(Path.home() / ".swing" / CONFIG_FILENAME)
    return paths


def find_example_config() -> Path | None:
    """Locate the committed ``config.example.toml`` if we are running from a checkout."""
    candidates = [Path.cwd() / EXAMPLE_FILENAME]
    here = Path(__file__).resolve()
    candidates.extend(parent / EXAMPLE_FILENAME for parent in here.parents[:4])
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"{path} is not valid TOML and could not be read: {exc}. "
            f"Compare it with config.example.toml — a missing quote or bracket is the usual cause."
        ) from exc
    except OSError as exc:
        raise ConfigError(f"{path} could not be opened: {exc}.") from exc


def _build_section(name: str, cls: type, raw: Any, source: Path | None) -> Any:
    if not isinstance(raw, dict):
        raise ConfigError(
            f"The [{name}] section of {source or 'your config'} must be a table of settings, "
            f"but it is a {type(raw).__name__}."
        )
    valid = {f.name for f in fields(cls)}
    unknown = sorted(set(raw) - valid)
    if unknown:
        raise ConfigError(
            f"Unknown setting{'s' if len(unknown) > 1 else ''} "
            f"{', '.join(repr(u) for u in unknown)} in the [{name}] section of "
            f"{source or 'your config'}. Valid settings for [{name}] are: "
            f"{', '.join(sorted(valid))}."
        )
    return cls(**raw)


def _anchor_reports_dir(cfg: Config, anchor: Path | None) -> Config:
    """Resolve a relative ``paths.reports_dir`` against the config file's directory.

    ``state_dir`` defaults to an absolute ``~/.swing`` while ``reports_dir``
    defaulted to the relative ``reports``, so the two halves of one run
    disagreed the moment the working directory changed: ``swing scan`` from the
    project wrote a report tree that ``swing confirm`` from ``~`` could not find
    (audit BUG-022). Anchoring to the file the setting was read from makes the
    path mean the same thing from anywhere.

    Running on example defaults (no config file at all) keeps the historical
    CWD-relative behaviour: there is no file to anchor to, and the loud
    "EXAMPLE DEFAULTS" warning already tells the user they are in a temporary
    situation.
    """
    if anchor is None:
        return cfg
    reports_dir = Path(cfg.paths.reports_dir)
    if reports_dir.is_absolute():
        return cfg
    resolved = Path(anchor) / reports_dir
    log.info("paths.reports_dir %s resolved against %s -> %s", reports_dir, anchor, resolved)
    return replace(cfg, paths=replace(cfg.paths, reports_dir=resolved))


def _build_config(
    data: dict[str, Any], source: Path | None, *, anchor: Path | None = None
) -> Config:
    unknown = sorted(set(data) - set(_SECTIONS))
    if unknown:
        raise ConfigError(
            f"Unknown section{'s' if len(unknown) > 1 else ''} "
            f"{', '.join('[' + u + ']' for u in unknown)} in {source or 'your config'}. "
            f"Valid sections are: {', '.join('[' + s + ']' for s in _SECTIONS)}."
        )
    kwargs: dict[str, Any] = {}
    for name, cls in _SECTIONS.items():
        if name in data:
            kwargs[name] = _build_section(name, cls, data[name], source)
    return _anchor_reports_dir(Config(**kwargs), anchor)


def load_config(path: Path | None = None) -> Config:
    """Load configuration, searching explicit path → ./config.toml → ~/.swing/config.toml.

    When nothing is found the committed example values (i.e. the dataclass
    defaults) are used and a loud warning is emitted, because running on
    defaults means running with a $100 paper account and no alert channels.

    A relative ``paths.reports_dir`` is resolved against the directory of the
    file it came from — see :func:`_anchor_reports_dir`. The example-defaults
    fallback is not a file the user chose, so it anchors nothing and the path
    stays relative to the working directory as before.

    Raises:
        ConfigError: if the file is missing, malformed, or holds a value that
            the system cannot honour. The message is a plain-English sentence.
    """
    if path is not None:
        candidate = Path(path).expanduser()
        if not candidate.is_file():
            raise ConfigError(
                f"No configuration file at {candidate}. Copy config.example.toml to that path "
                f"(or drop the --config flag to use ./config.toml)."
            )
        return _load_from(candidate)

    for candidate in config_search_paths():
        if candidate.is_file():
            return _load_from(candidate)

    example = find_example_config()
    warnings.warn(
        "No config.toml found (looked in ./config.toml and ~/.swing/config.toml), so swing is "
        "running on EXAMPLE DEFAULTS: a $100 account, no alert channels and no broker "
        "credentials. Copy config.example.toml to ./config.toml and edit it before trusting "
        "any output.",
        UserWarning,
        stacklevel=2,
    )
    if example is not None:
        log.info("Loading example defaults from %s", example)
        return _build_config(_read_toml(example), example)
    return Config()


def _load_from(candidate: Path) -> Config:
    """Read one config file and anchor its relative paths to that file's directory."""
    log.info("Loading configuration from %s", candidate)
    # `.absolute()` rather than `.resolve()`: a `--config config.toml` must
    # anchor to the working directory, but symlinks are left as the user wrote
    # them.
    return _build_config(_read_toml(candidate), candidate, anchor=candidate.absolute().parent)


def describe_defaults() -> dict[str, dict[str, Any]]:
    """Return every section's default values — used to keep the example file honest."""
    out: dict[str, dict[str, Any]] = {}
    for name, cls in _SECTIONS.items():
        section: dict[str, Any] = {}
        for f in fields(cls):
            if f.default is not MISSING:
                section[f.name] = f.default
            elif f.default_factory is not MISSING:  # type: ignore[misc]
                section[f.name] = f.default_factory()  # type: ignore[misc]
        out[name] = section
    return out
