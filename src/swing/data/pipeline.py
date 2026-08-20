"""Cache maintenance: backfill, incremental update, status.

The nightly scan calls :func:`update`; a fresh install calls :func:`backfill`
once. Both are idempotent, and both are written so a re-run with nothing new to
fetch performs **zero network calls** — that property is what makes the whole
system usable when Yahoo is having a bad day.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from ..config import Config
from ..logging_setup import get_logger
from .cache import BarCache, ProviderMismatch, merge_bars
from .provider import DataProvider, get_provider, normalize_bars
from .universe import Symbol, build_universe

log = get_logger("swing.data.pipeline")

#: How far a re-fetched close may differ from the cached close for the same day
#: before the series is treated as re-based rather than revised. A dividend
#: re-adjustment moves every prior close by 0.3-0.6% and a split moves them by
#: multiples, so 0.1% sits well under the smallest event worth catching and
#: well over parquet round-trip noise.
REBASE_TOLERANCE = 0.001

#: Above this share of empty responses, "no data" means the provider is having
#: an outage, not that the symbols are delisted — and marking them absent would
#: hide them from every scan for `absent_retry_days`. Not a config key on
#: purpose: it is a floor under a footgun, not a preference to tune.
OUTAGE_ABSENT_FRACTION = 0.20


def _cache(cfg: Config) -> BarCache:
    return BarCache(cfg.expand_path(cfg.data.cache_dir))


def ensure_provider(cfg: Config, cache: BarCache) -> str:
    """Bind the cache to one provider, or refuse to write into it.

    Called before anything fetches. An unstamped cache (a fresh install, or one
    that predates stamping) adopts the configured provider silently — that is
    the migration path, and it is safe because whatever is already there was
    written by whatever was configured then, which is the same thing the user
    is running now. A *disagreeing* stamp raises: see ProviderMismatch for why
    this cannot be a warning.
    """
    name = str(cfg.data.provider).lower()
    stamped = cache.stamped_provider()

    if stamped is None:
        if cache.symbols():
            log.info("stamping the existing cache as written by %r", name)
        cache.stamp_provider(name)
        return name

    if stamped == name:
        return name

    raise ProviderMismatch(
        f"this cache was written by the {stamped!r} provider, but [data] provider "
        f"is now {name!r}.\n"
        f"  cache: {cache.root}\n"
        "Providers adjust prices differently (yfinance adjusts for dividends, "
        "Stooq does not), so appending one to the other would splice two price "
        "series into one and quietly corrupt every indicator, backtest and stop "
        "derived from it.\n"
        "To switch providers, delete the cache and re-download:\n"
        f"  rm -rf {cache.root} && swing data --backfill\n"
        "To keep the existing data, set [data] provider back to "
        f"{stamped!r}."
    )


def _symbols_for(cfg: Config, symbols: list[str] | None) -> list[str]:
    if symbols:
        return [s.upper() for s in symbols]
    universe = build_universe(cfg)
    out = [s.symbol for s in universe]
    regime = str(cfg.strategy.regime.get("symbol", "SPY")).upper()
    if regime not in out:
        out.append(regime)          # the regime filter needs its own bars
    return out


def backfill(
    cfg: Config,
    symbols: list[str] | None = None,
    provider: DataProvider | None = None,
) -> dict[str, int]:
    """Download full history for every symbol that lacks it."""
    cache = _cache(cfg)
    ensure_provider(cfg, cache)
    provider = provider or get_provider(cfg)
    start = date.fromisoformat(str(cfg.data.history_start))
    today = date.today()

    wanted = _symbols_for(cfg, symbols)
    retry_days = int(cfg.data.get("absent_retry_days", 7))
    skip = cache.absent_symbols(retry_days, as_of=today) if not symbols else set()
    missing = [s for s in wanted if not cache.has(s) and s not in skip]
    log.info(
        "backfill: %d requested, %d cached, %d known-absent, %d to download",
        len(wanted), sum(1 for s in wanted if cache.has(s)), len(skip & set(wanted)),
        len(missing),
    )
    if not missing:
        return {}

    fetched = provider.daily_bars(missing, start, today)
    counts: dict[str, int] = {}
    for sym, bars in fetched.items():
        cache.write(sym, bars)
        counts[sym] = len(bars)
    cache.clear_absent(list(fetched))
    absent = sorted(set(missing) - set(fetched))
    if absent and len(absent) > OUTAGE_ABSENT_FRACTION * len(missing):
        # Epidemic absence is a provider outage wearing a delisting's clothes.
        # Recording it would make the *next* run skip these symbols too, so one
        # bad five minutes costs the universe a week of invisibility.
        log.warning(
            "%d of %d symbols returned no data (%.0f%%) — treating that as a "
            "provider outage, not %d delistings; nothing marked absent, so the "
            "next run retries them",
            len(absent), len(missing), 100.0 * len(absent) / len(missing), len(absent),
        )
    elif absent:
        cache.mark_absent(absent, when=today)
        log.warning(
            "%d symbols returned no data (delisted, renamed, or provider hiccup); "
            "will not retry for %d days: %s",
            len(absent), retry_days,
            ", ".join(absent[:15]) + (" ..." if len(absent) > 15 else ""),
        )
    log.info("backfill complete: %d symbols written", len(counts))
    return counts


def update(
    cfg: Config,
    symbols: list[str] | None = None,
    provider: DataProvider | None = None,
    as_of: date | None = None,
) -> dict[str, int]:
    """Fetch only the bars that are missing since each symbol's last cached day.

    Symbols already current are skipped entirely, so an unchanged re-run makes
    no network calls at all.
    """
    cache = _cache(cfg)
    ensure_provider(cfg, cache)
    as_of = as_of or date.today()
    wanted = _symbols_for(cfg, symbols)

    retry_days = int(cfg.data.get("absent_retry_days", 7))
    skip = cache.absent_symbols(retry_days, as_of=as_of) if not symbols else set()

    stale: dict[str, date] = {}
    never_seen: list[str] = []
    for sym in wanted:
        if sym in skip:
            continue
        last = cache.last_date(sym)
        if last is None:
            never_seen.append(sym)
        elif last < as_of:
            stale[sym] = last

    if never_seen:
        log.info("%d symbols have no cache yet; backfilling those first", len(never_seen))
        provider = provider or get_provider(cfg)
        backfill(cfg, symbols=never_seen, provider=provider)
        # backfill() records anything still empty; honour that immediately so
        # the stale-window fetch below does not ask for them again.
        skip |= cache.absent_symbols(retry_days, as_of=as_of)
        stale = {k: v for k, v in stale.items() if k not in skip}

    if not stale:
        log.info("cache already current through %s; no requests made", as_of)
        return {}

    provider = provider or get_provider(cfg)
    # One window covering the oldest stale symbol keeps this to a few batched
    # requests instead of one request per symbol. The overlap is not just
    # harmless, it is the detector: rows for days the cache already holds are
    # what reveal a vendor re-adjustment before it can be grafted on.
    oldest = min(stale.values())
    start = oldest - timedelta(days=5)
    log.info("updating %d symbols from %s", len(stale), start)

    fetched = provider.daily_bars(sorted(stale), start, as_of)
    counts: dict[str, int] = {}
    rebased: list[str] = []
    for sym, bars in fetched.items():
        fresh = normalize_bars(bars)
        cached = cache.read(sym)
        drift, when = _overlap_drift(cached, fresh)
        if drift > REBASE_TOLERANCE:
            log.warning(
                "%s: re-fetched closes disagree with the cache by %.2f%% on %s — "
                "the vendor re-based the whole series (dividend or split), so "
                "merging this window would splice two price paths; re-downloading "
                "its full history instead",
                sym, drift * 100.0, when,
            )
            rebased.append(sym)
            continue
        merged = merge_bars(cached, fresh)
        cache.write(sym, merged)
        counts[sym] = len(merged)

    if rebased:
        counts.update(_redownload(cfg, cache, provider, rebased, as_of))
    return counts


def _overlap_drift(cached: pd.DataFrame, fresh: pd.DataFrame) -> tuple[float, date | None]:
    """Largest relative close disagreement on days both frames carry.

    ``(0.0, None)`` when they share no day: no overlap is no evidence, and the
    house rule is that a data path degrades rather than refuses.
    """
    if not len(cached) or not len(fresh):
        return 0.0, None
    overlap = cached.index.intersection(fresh.index)
    if not len(overlap):
        return 0.0, None

    old = cached.loc[overlap, "close"]
    diff = (fresh.loc[overlap, "close"] - old).abs() / old
    diff = diff.replace([float("inf"), float("-inf")], 0.0).fillna(0.0)
    worst = diff.idxmax()
    return float(diff.loc[worst]), worst.date()


def _redownload(
    cfg: Config,
    cache: BarCache,
    provider: DataProvider,
    symbols: list[str],
    as_of: date,
) -> dict[str, int]:
    """Replace whole histories for symbols whose adjustment basis moved.

    Only the symbols that actually diverged pay for this, which is what keeps a
    corporate action costing one full download rather than a nightly re-backfill
    of the universe.
    """
    start = date.fromisoformat(str(cfg.data.history_start))
    log.info("re-downloading %d re-based symbol(s) from %s", len(symbols), start)
    full = provider.daily_bars(sorted(symbols), start, as_of)

    counts: dict[str, int] = {}
    for sym in sorted(symbols):
        bars = normalize_bars(full[sym]) if sym in full else None
        if bars is None or not len(bars):
            # A stale cache on one consistent basis still produces usable
            # indicators; a spliced one does not. Leave it and retry tomorrow.
            log.warning(
                "%s: full re-download returned no bars; leaving the cached "
                "history on its old basis rather than splicing", sym,
            )
            continue
        cache.write(sym, bars)
        counts[sym] = len(bars)
    return counts


def clear_absent(cfg: Config, symbols: list[str] | None = None) -> list[str]:
    """Forget that ``symbols`` (or every symbol) ever came back empty.

    The escape hatch for the negative cache: the outage guard in :func:`backfill`
    stops it filling up during a bad night, and this empties it when something
    got in there anyway. Returns the symbols actually cleared.
    """
    cache = _cache(cfg)
    known = cache.read_absent()
    targets = [s.upper() for s in symbols] if symbols else sorted(known)
    cleared = [s for s in targets if s in known]
    cache.clear_absent(cleared)
    return cleared


def refresh_earnings(
    cfg: Config,
    symbols: list[str] | None = None,
    provider: DataProvider | None = None,
    max_age_days: float = 3.0,
) -> dict[str, date | None]:
    """Refresh cached earnings dates if the cache is older than ``max_age_days``."""
    cache = _cache(cfg)
    age = cache.earnings_age_days()
    if age is not None and age < max_age_days:
        log.info("earnings cache is %.1f days old; reusing", age)
        return cache.read_earnings()

    provider = provider or get_provider(cfg)
    wanted = _symbols_for(cfg, symbols)
    log.info("fetching earnings dates for %d symbols (this is the slow part)", len(wanted))
    dates = provider.earnings_dates(wanted)
    known = sum(1 for v in dates.values() if v)
    log.info("earnings dates: %d known, %d unknown", known, len(dates) - known)
    cache.write_earnings(dates)
    return dates


def refresh_fundamentals(
    cfg: Config,
    symbols: list[str] | None = None,
    provider: DataProvider | None = None,
    max_age_days: float = 7.0,
):
    """Refresh cached fundamentals if stale. Fundamentals move slowly; weekly is plenty."""
    cache = _cache(cfg)
    age = cache.fundamentals_age_days()
    if age is not None and age < max_age_days:
        log.info("fundamentals cache is %.1f days old; reusing", age)
        return cache.read_fundamentals()

    provider = provider or get_provider(cfg)
    wanted = _symbols_for(cfg, symbols)
    log.info("fetching fundamentals for %d symbols", len(wanted))
    funds = provider.fundamentals(wanted)
    cache.write_fundamentals(funds)
    return funds


def load_bars(
    cfg: Config,
    symbols: list[Symbol] | list[str] | None = None,
    start: date | None = None,
    end: date | None = None,
    min_bars: int = 0,
) -> dict[str, pd.DataFrame]:
    """Read bars straight out of the cache (no network), sliced to a window."""
    cache = _cache(cfg)
    if symbols is None:
        names = _symbols_for(cfg, None)
    else:
        names = [s.symbol if isinstance(s, Symbol) else str(s).upper() for s in symbols]

    out: dict[str, pd.DataFrame] = {}
    for sym in names:
        bars = cache.read(sym)
        if not len(bars):
            continue
        if start is not None:
            bars = bars[bars.index >= pd.Timestamp(start)]
        if end is not None:
            bars = bars[bars.index <= pd.Timestamp(end)]
        if len(bars) >= min_bars:
            out[sym] = bars
    return out


def cache_status(cfg: Config) -> str:
    cache = _cache(cfg)
    cov = cache.coverage()
    if not len(cov):
        return (
            f"cache at {cache.root}: empty\n"
            "run `swing data --backfill` to populate it."
        )
    newest = cov["end"].max()
    oldest_end = cov["end"].min()
    stale_limit = int(cfg.data.get("max_stale_days", 5))
    stale = cov[cov["end"] < (newest - timedelta(days=stale_limit))]

    stamped = cache.stamped_provider()
    configured = str(cfg.data.provider).lower()
    provider_line = stamped or "unstamped (adopts the configured provider on next write)"
    if stamped and stamped != configured:
        provider_line += f"  <-- MISMATCH: [data] provider is now {configured!r}"

    lines = [
        f"cache at {cache.root}",
        f"  provider    {provider_line}",
        f"  symbols     {len(cov)}",
        f"  bars        {int(cov['bars'].sum()):,}",
        f"  history     {cov['start'].min()} -> {newest}",
        f"  oldest tip  {oldest_end}",
        f"  stale       {len(stale)} symbols more than {stale_limit} days behind",
    ]
    if len(stale):
        lines.append("              " + ", ".join(list(stale.index[:10])))
    age = cache.earnings_age_days()
    lines.append(f"  earnings    {'never fetched' if age is None else f'{age:.1f} days old'}")
    age = cache.fundamentals_age_days()
    lines.append(f"  fundamentals {'never fetched' if age is None else f'{age:.1f} days old'}")
    return "\n".join(lines)
