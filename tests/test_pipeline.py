"""Pipeline integrity: what the nightly update is allowed to write into the cache.

Two invariants live here, and both are about the same thing — a cached series
must be one price path on one adjustment basis:

* a vendor that re-bases its history (dividend, split) must not get its new
  basis grafted onto the old one, so the five-day overlap the update already
  fetches is compared before anything is merged;
* a provider outage must not be recorded as a universe full of delistings, or
  one bad five minutes hides those symbols from every scan for a week.

The third member of that family — Schwab bars never arriving from yfinance,
because the cache is stamped with the *configured* provider — is pinned at the
provider itself, in ``tests/test_auth.py``.

No network: every provider here is a canned in-memory fake.
"""

from __future__ import annotations

import logging
from datetime import date

import pandas as pd
import pytest

from swing.config import Config
from swing.data.cache import BarCache
from swing.data.pipeline import backfill, clear_absent, update

from .conftest import FakeProvider, trending_bars

PRICE_COLUMNS = ["open", "high", "low", "close"]


def _cache_of(cfg: Config) -> BarCache:
    return BarCache(cfg.expand_path(cfg.data.cache_dir))


def _history_start(cfg: Config) -> date:
    return date.fromisoformat(str(cfg.data.history_start))


def _rebased(bars: pd.DataFrame, factor: float) -> pd.DataFrame:
    """The same series on a new adjustment basis: every price scaled, forever.

    This is what ``auto_adjust=True`` does after a dividend or a split — it does
    not append a corrected bar, it restates the whole history.
    """
    out = bars.copy()
    out[PRICE_COLUMNS] = out[PRICE_COLUMNS] * factor
    return out


def _universe_of(cfg: Config, n: int) -> Config:
    """A config whose universe is ``n`` symbols (the regime symbol joins them)."""
    data = cfg.as_dict()
    data["universe"]["extra_symbols"] = [f"S{i:02d}" for i in range(n)]
    return Config(data)


class WindowProvider:
    """Serves slices of whatever series it currently holds, and records calls.

    The distinction that matters for re-adjustment: this fake does not remember
    what it handed out last night. Ask it for any window after a re-basing and
    you get the new basis, including the days the cache already has — which is
    exactly how the real thing behaves, and why the overlap is a detector.
    """

    name = "fake"

    def __init__(self, bars: dict[str, pd.DataFrame]):
        self._bars = dict(bars)
        self.calls: list[tuple[tuple[str, ...], date, date]] = []
        #: Start dates at or before this come back empty (a re-download that fails).
        self.refuse_from: date | None = None

    def serve(self, bars: dict[str, pd.DataFrame]) -> None:
        self._bars = dict(bars)

    def daily_bars(self, symbols, start, end):
        self.calls.append((tuple(sorted(symbols)), start, end))
        if self.refuse_from is not None and start <= self.refuse_from:
            return {}
        out = {}
        for sym in symbols:
            frame = self._bars.get(sym)
            if frame is None:
                continue
            window = frame[
                (frame.index >= pd.Timestamp(start)) & (frame.index <= pd.Timestamp(end))
            ]
            if len(window):
                out[sym] = window
        return out

    def quotes(self, symbols):
        return {}

    def earnings_dates(self, symbols):
        return {s: None for s in symbols}

    def fundamentals(self, symbols):
        return {}

    def full_history_calls(self, cfg: Config):
        """Calls that asked for a whole history — i.e. a re-download."""
        return [c for c in self.calls if c[1] == _history_start(cfg)]


@pytest.fixture
def seeded(base_config):
    """250 cached bars for the whole universe, with 300 available upstream.

    The call log is cleared afterwards so every assertion below is about what
    the *update* did.
    """
    full = trending_bars(n=300, start="2020-01-01")
    provider = WindowProvider({s: full.iloc[:250] for s in ("AAA", "BBB", "SPY")})
    backfill(base_config, provider=provider)
    provider.calls.clear()
    return provider, full


# ---------------------------------------------------------------------------
# adjustment-basis revisions
# ---------------------------------------------------------------------------
def test_a_re_based_series_is_re_downloaded_not_grafted(base_config, seeded):
    """The whole point: one basis end to end, no step at the merge boundary."""
    provider, full = seeded
    provider.serve({s: _rebased(full, 0.99) for s in ("AAA", "BBB", "SPY")})

    update(base_config, provider=provider, as_of=full.index[-1].date())

    cached = _cache_of(base_config).read("AAA")
    assert len(cached) == 300
    pd.testing.assert_series_equal(
        cached["close"], _rebased(full, 0.99)["close"], check_freq=False
    )

    # Measured against the old basis, every bar moved by the same factor. A
    # graft would leave the pre-window rows at 1.0 and the rest at 0.99.
    ratio = cached["close"] / full["close"].reindex(cached.index)
    assert ratio.max() - ratio.min() < 1e-9


def test_an_agreeing_overlap_still_does_the_cheap_merge(base_config, seeded):
    """No re-basing means no re-download: one window fetch, extended in place."""
    provider, full = seeded
    provider.serve({s: full for s in ("AAA", "BBB", "SPY")})

    update(base_config, provider=provider, as_of=full.index[-1].date())

    assert len(provider.calls) == 1                 # one window, no re-download
    assert provider.full_history_calls(base_config) == []
    assert provider.calls[0][1] > date(2020, 1, 1)  # only the stale tail was asked for
    assert len(_cache_of(base_config).read("AAA")) == 300


def test_only_the_re_based_symbol_pays_for_a_full_re_download(base_config, seeded):
    """A corporate action costs one symbol's history, not the universe's."""
    provider, full = seeded
    provider.serve({"AAA": _rebased(full, 0.5), "BBB": full, "SPY": full})

    update(base_config, provider=provider, as_of=full.index[-1].date())

    full_calls = provider.full_history_calls(base_config)
    assert len(full_calls) == 1
    assert full_calls[0][0] == ("AAA",)
    assert len(_cache_of(base_config).read("BBB")) == 300


def test_the_divergence_warning_names_the_symbol(base_config, seeded, caplog):
    """A 10:1 split is the loudest version of this; it must not pass silently."""
    provider, full = seeded
    provider.serve({"AAA": _rebased(full, 0.1), "BBB": full, "SPY": full})

    with caplog.at_level(logging.WARNING, logger="swing.data.pipeline"):
        update(base_config, provider=provider, as_of=full.index[-1].date())

    warnings = [
        r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
    ]
    assert any("AAA" in m and "%" in m for m in warnings)


def test_a_revision_below_tolerance_is_merged_not_re_downloaded(base_config, seeded):
    """Vendors nudge the last print; that is a revision, not a new basis."""
    provider, full = seeded
    provider.serve({s: _rebased(full, 1.0005) for s in ("AAA", "BBB", "SPY")})

    update(base_config, provider=provider, as_of=full.index[-1].date())

    assert provider.full_history_calls(base_config) == []
    assert len(_cache_of(base_config).read("AAA")) == 300


def test_a_failed_re_download_leaves_the_old_basis_alone(base_config, seeded):
    """Stale on one basis is usable; spliced is not. Tomorrow tries again."""
    provider, full = seeded
    provider.serve({s: _rebased(full, 0.5) for s in ("AAA", "BBB", "SPY")})
    provider.refuse_from = _history_start(base_config)

    update(base_config, provider=provider, as_of=full.index[-1].date())

    cached = _cache_of(base_config).read("AAA")
    assert len(cached) == 250                       # nothing was grafted on
    pd.testing.assert_series_equal(
        cached["close"], full.iloc[:250]["close"], check_freq=False
    )


def test_a_symbol_with_no_overlap_at_all_is_still_merged(base_config):
    """No shared day is no evidence — and a data path degrades, it does not refuse."""
    early = trending_bars(n=20, start="2020-01-01")
    later = trending_bars(n=20, start="2021-06-01", start_price=500.0)
    provider = WindowProvider({"AAA": early})
    backfill(base_config, symbols=["AAA"], provider=provider)
    provider.calls.clear()

    provider.serve({"AAA": later})
    update(base_config, symbols=["AAA"], provider=provider,
           as_of=later.index[-1].date())

    assert len(_cache_of(base_config).read("AAA")) == 40
    assert provider.full_history_calls(base_config) == []


# ---------------------------------------------------------------------------
# the negative cache under an outage
# ---------------------------------------------------------------------------
def test_an_epidemic_of_empty_responses_marks_nothing_absent(base_config, caplog):
    """A provider having a bad five minutes must not cost the universe a week."""
    cfg = _universe_of(base_config, 9)              # + SPY = 10 requested
    provider = FakeProvider(bars={"S00": trending_bars(n=50)})

    with caplog.at_level(logging.WARNING, logger="swing.data.pipeline"):
        backfill(cfg, provider=provider)

    cache = _cache_of(cfg)
    assert cache.read_absent() == {}
    assert any("outage" in r.getMessage() for r in caplog.records)

    # ...and because nothing was recorded, the next run asks for them again.
    backfill(cfg, provider=provider)
    assert len(provider.bar_calls) == 2
    assert "S08" in provider.bar_calls[1][0]


def test_a_handful_of_dead_tickers_is_still_recorded(base_config):
    """The negative cache still does its job below the outage threshold."""
    cfg = _universe_of(base_config, 9)               # + SPY = 10 requested
    served = [f"S{i:02d}" for i in range(8)] + ["SPY"]
    provider = FakeProvider(bars={s: trending_bars(n=50) for s in served})

    backfill(cfg, provider=provider)

    cache = _cache_of(cfg)
    assert set(cache.read_absent()) == {"S08"}

    backfill(cfg, provider=provider)
    assert len(provider.bar_calls) == 1              # S08 was not re-requested


def test_exactly_the_threshold_still_counts_as_delistings(base_config):
    """20% is the boundary: *more* than that is an outage, 20% itself is not."""
    cfg = _universe_of(base_config, 9)               # + SPY = 10 requested
    served = [f"S{i:02d}" for i in range(7)] + ["SPY"]
    provider = FakeProvider(bars={s: trending_bars(n=50) for s in served})

    backfill(cfg, provider=provider)

    assert set(_cache_of(cfg).read_absent()) == {"S07", "S08"}


def test_the_outage_guard_counts_what_was_asked_for_not_the_universe(base_config):
    """Symbols already cached are not evidence about the provider's health."""
    cfg = _universe_of(base_config, 9)
    served = [f"S{i:02d}" for i in range(9)] + ["SPY"]
    provider = FakeProvider(bars={s: trending_bars(n=50) for s in served})
    backfill(cfg, provider=provider)                 # everything cached

    # One new symbol, and it comes back empty: 1 of 1 tried, not 1 of 11.
    cfg = _universe_of(base_config, 10)
    backfill(cfg, provider=provider)
    assert _cache_of(cfg).read_absent() == {}


# ---------------------------------------------------------------------------
# the escape hatch
# ---------------------------------------------------------------------------
def test_clear_absent_forgets_only_the_named_symbols(base_config):
    cache = _cache_of(base_config)
    cache.mark_absent(["BK", "CTRA", "ZZZZ"], when=date(2026, 8, 19))

    assert clear_absent(base_config, ["bk"]) == ["BK"]
    assert set(cache.read_absent()) == {"CTRA", "ZZZZ"}


def test_clear_absent_with_no_names_empties_the_list(base_config):
    cache = _cache_of(base_config)
    cache.mark_absent(["BK", "CTRA"], when=date(2026, 8, 19))

    assert clear_absent(base_config) == ["BK", "CTRA"]
    assert cache.read_absent() == {}


def test_clearing_a_symbol_that_was_never_absent_is_not_an_error(base_config):
    assert clear_absent(base_config, ["AAA"]) == []


def test_a_cleared_symbol_is_downloaded_again_on_the_next_run(base_config):
    """The whole reason the hatch exists: BK is in the S&P 500, not delisted."""
    cfg = _universe_of(base_config, 9)
    served = [f"S{i:02d}" for i in range(8)] + ["SPY"]
    provider = FakeProvider(bars={s: trending_bars(n=50) for s in served})
    backfill(cfg, provider=provider)
    assert set(_cache_of(cfg).read_absent()) == {"S08"}

    clear_absent(cfg, ["S08"])
    provider._bars["S08"] = trending_bars(n=50)
    backfill(cfg, provider=provider)
    assert _cache_of(cfg).has("S08")


def test_clear_absent_is_wired_through_the_cli(tmp_path, capsys):
    """Proves the argparse wiring, not just the function."""
    from swing.cli import main

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("[data]\n" f'cache_dir = "{tmp_path / "cache"}"\n')
    cache = BarCache(tmp_path / "cache")
    cache.mark_absent(["BK", "CTRA"], when=date(2026, 8, 19))

    assert main(["-c", str(cfg_path), "data", "--clear-absent", "BK"]) == 0
    out = capsys.readouterr().out
    assert "BK" in out and "CTRA" not in out
    assert set(cache.read_absent()) == {"CTRA"}

    assert main(["-c", str(cfg_path), "data", "--clear-absent"]) == 0
    assert "CTRA" in capsys.readouterr().out
    assert cache.read_absent() == {}


def test_clear_absent_on_an_empty_list_says_so(tmp_path, capsys):
    from swing.cli import main

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("[data]\n" f'cache_dir = "{tmp_path / "cache"}"\n')

    assert main(["-c", str(cfg_path), "data", "--clear-absent"]) == 0
    assert "nothing to clear" in capsys.readouterr().out
