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
"""

from __future__ import annotations

import datetime as _dt
import logging
import tomllib
import warnings
from dataclasses import MISSING, dataclass, field, fields
from pathlib import Path
from typing import Any, get_args, get_origin

__all__ = [
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


def _coerce_value(value: Any, ann: Any, cls_name: str, key: str) -> Any:
    if value is None:
        return None
    inner = _optional_inner(ann)
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
    """Where price history comes from and where it is cached."""

    provider: str = "yfinance"
    cache_dir: Path = field(default_factory=lambda: Path.home() / ".swing" / "cache")
    start_date: _dt.date = _dt.date(2010, 1, 1)

    def __post_init__(self) -> None:
        _coerce(self)
        _one_of(self.provider, ("yfinance", "schwab"), "data", "provider")
        _require(
            self.start_date >= _dt.date(1970, 1, 1),
            f"data.start_date must be 1970-01-01 or later, but it is {self.start_date}.",
            "data",
            "start_date",
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
        _in_range(self.adx_min, 0.0, 100.0, "strategy", "adx_min", "minimum ADX trend strength")
        _in_range(
            self.breakout_proximity_pct,
            0.0,
            100.0,
            "strategy",
            "breakout_proximity_pct",
            "how close to the breakout level still counts, in percent",
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

    def __post_init__(self) -> None:
        _coerce(self)
        if self.end is not None:
            _require(
                self.start < self.end,
                f"backtest.start ({self.start}) must be earlier than backtest.end ({self.end}).",
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


def _build_config(data: dict[str, Any], source: Path | None) -> Config:
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
    return Config(**kwargs)


def load_config(path: Path | None = None) -> Config:
    """Load configuration, searching explicit path → ./config.toml → ~/.swing/config.toml.

    When nothing is found the committed example values (i.e. the dataclass
    defaults) are used and a loud warning is emitted, because running on
    defaults means running with a $100 paper account and no alert channels.

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
        log.info("Loading configuration from %s", candidate)
        return _build_config(_read_toml(candidate), candidate)

    for candidate in config_search_paths():
        if candidate.is_file():
            log.info("Loading configuration from %s", candidate)
            return _build_config(_read_toml(candidate), candidate)

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
        return _build_config(_read_toml(example), example)
    return Config()


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
