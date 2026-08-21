"""Cache round-trips, merge semantics, and the zero-network re-run property."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from swing.config import Config
from swing.data.cache import BarCache, data_fingerprint, merge_bars
from swing.data.pipeline import backfill, load_bars, update
from swing.data.provider import normalize_bars

from .conftest import FakeProvider, make_bars, trending_bars


def test_round_trip_preserves_values(tmp_path):
    cache = BarCache(tmp_path)
    bars = trending_bars(n=50)
    cache.write("AAA", bars)
    back = cache.read("AAA")
    pd.testing.assert_frame_equal(back, bars, check_freq=False)


def test_read_missing_symbol_is_empty_not_an_error(tmp_path):
    cache = BarCache(tmp_path)
    assert len(cache.read("NOPE")) == 0
    assert cache.last_date("NOPE") is None


def test_corrupt_parquet_is_treated_as_a_miss(tmp_path):
    cache = BarCache(tmp_path)
    cache.write("AAA", trending_bars(n=30))
    cache.path_for("AAA").write_bytes(b"not a parquet file")
    assert len(cache.read("AAA")) == 0


def test_merge_prefers_fresh_rows_on_overlap():
    old = make_bars([10.0, 11.0, 12.0], start="2020-01-01")
    new = make_bars([99.0, 98.0], start="2020-01-02")
    merged = merge_bars(old, new)
    assert len(merged) == 3
    # 2020-01-02 and -03 came from `new`
    assert merged["close"].tolist() == [10.0, 99.0, 98.0]


def test_merge_with_empty_sides():
    bars = trending_bars(n=10)
    assert len(merge_bars(bars, bars.iloc[0:0])) == 10
    assert len(merge_bars(bars.iloc[0:0], bars)) == 10


def test_normalize_handles_alternate_spellings():
    raw = pd.DataFrame(
        {
            "Open": [1.0, 2.0],
            "High": [2.0, 3.0],
            "Low": [0.5, 1.5],
            "Adj Close": [1.5, 2.5],
            "Volume": [100, 200],
        },
        index=pd.to_datetime(["2020-01-02", "2020-01-03"]),
    )
    out = normalize_bars(raw)
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]
    assert out["close"].tolist() == [1.5, 2.5]
    assert out.index.name == "date"


def test_normalize_drops_nonpositive_and_duplicate_rows():
    idx = pd.to_datetime(["2020-01-02", "2020-01-02", "2020-01-03"])
    raw = pd.DataFrame(
        {"open": [1, 1, 1], "high": [1, 1, 1], "low": [1, 1, 1],
         "close": [1.0, 5.0, 0.0], "volume": [1, 1, 1]},
        index=idx,
    )
    out = normalize_bars(raw)
    assert len(out) == 1
    assert out["close"].iloc[0] == 5.0     # last duplicate wins, zero-price row dropped


def test_normalize_strips_timezone():
    idx = pd.to_datetime(["2020-01-02T00:00:00-05:00"])
    raw = pd.DataFrame(
        {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1.0]},
        index=idx,
    )
    assert normalize_bars(raw).index.tz is None


def test_backfill_then_update_makes_zero_calls_when_current(base_config):
    bars = {"AAA": trending_bars(n=300, start="2020-01-01"),
            "BBB": trending_bars(n=300, start="2020-01-01", seed=1),
            "SPY": trending_bars(n=300, start="2020-01-01", seed=2)}
    last = bars["AAA"].index[-1].date()
    provider = FakeProvider(bars=bars)

    backfill(base_config, provider=provider)
    assert len(provider.bar_calls) == 1

    # Everything is current as of the last cached bar -> no further requests.
    update(base_config, provider=provider, as_of=last)
    assert len(provider.bar_calls) == 1


def test_backfill_skips_symbols_already_cached(base_config):
    provider = FakeProvider(
        bars={s: trending_bars(n=100) for s in ("AAA", "BBB", "SPY")}
    )
    backfill(base_config, provider=provider)
    backfill(base_config, provider=provider)
    assert len(provider.bar_calls) == 1     # second call had nothing to fetch


def _universe_of(cfg: Config, n: int) -> Config:
    """A config whose universe is ``n`` symbols (the regime symbol joins them).

    Wide enough that one dead ticker stays under the outage threshold: above it,
    empty responses mean "the provider is down" and nothing is recorded at all
    (tests/test_pipeline.py owns that boundary).
    """
    data = cfg.as_dict()
    data["universe"]["extra_symbols"] = [f"S{i:02d}" for i in range(n)]
    return Config(data)


def test_symbols_that_return_nothing_are_not_re_requested(base_config):
    """A delisted ticker must not be re-downloaded every night."""
    cfg = _universe_of(base_config, 9)                       # + SPY = 10 requested
    served = [f"S{i:02d}" for i in range(8)] + ["SPY"]
    provider = FakeProvider(bars={s: trending_bars(n=100) for s in served})

    backfill(cfg, provider=provider)
    assert "S08" in provider.bar_calls[0][0]

    backfill(cfg, provider=provider)
    assert len(provider.bar_calls) == 1

    # ...but the skip expires, so a provider outage is not permanent.
    cache = BarCache(cfg.expand_path(cfg.data.cache_dir))
    assert cache.absent_symbols(retry_after_days=0) == set()


def test_update_fetches_and_extends(base_config):
    full = trending_bars(n=300, start="2020-01-01")
    provider = FakeProvider(
        bars={s: full.iloc[:250] for s in ("AAA", "BBB", "SPY")}
    )
    backfill(base_config, provider=provider)

    cache = BarCache(base_config.expand_path(base_config.data.cache_dir))
    assert len(cache.read("AAA")) == 250

    provider._bars = {s: full for s in ("AAA", "BBB", "SPY")}
    update(base_config, provider=provider, as_of=full.index[-1].date())
    assert len(cache.read("AAA")) == 300


def test_update_backfills_symbols_that_have_no_cache(base_config):
    provider = FakeProvider(
        bars={s: trending_bars(n=100) for s in ("AAA", "BBB", "SPY")}
    )
    update(base_config, provider=provider, as_of=date(2021, 1, 1))
    cache = BarCache(base_config.expand_path(base_config.data.cache_dir))
    assert cache.has("AAA") and cache.has("BBB")


def test_load_bars_slices_window(base_config):
    provider = FakeProvider(bars={"AAA": trending_bars(n=300, start="2020-01-01")})
    backfill(base_config, symbols=["AAA"], provider=provider)
    out = load_bars(base_config, ["AAA"], start=date(2020, 6, 1), end=date(2020, 6, 30))
    assert 15 <= len(out["AAA"]) <= 23
    assert out["AAA"].index[0] >= pd.Timestamp("2020-06-01")


def test_fingerprint_changes_with_data_not_with_dict_order():
    a = trending_bars(n=50)
    b = trending_bars(n=50, seed=7, noise=0.01)
    assert data_fingerprint({"X": a, "Y": b}) == data_fingerprint({"Y": b, "X": a})
    assert data_fingerprint({"X": a}) != data_fingerprint({"X": b})


@pytest.mark.parametrize("n", [0, 1, 5])
def test_coverage_handles_small_and_empty_caches(tmp_path, n):
    cache = BarCache(tmp_path)
    if n:
        cache.write("AAA", trending_bars(n=n))
    cov = cache.coverage()
    assert len(cov) == (1 if n else 0)


def test_update_does_not_re_request_known_absent_symbols(base_config):
    """The nightly update must not hammer the provider for dead tickers."""
    cfg = _universe_of(base_config, 9)                       # + SPY = 10 requested
    served = [f"S{i:02d}" for i in range(8)] + ["SPY"]
    provider = FakeProvider(
        bars={s: trending_bars(n=100, start="2020-01-01") for s in served}
    )
    last = provider._bars["SPY"].index[-1].date()

    update(cfg, provider=provider, as_of=last)
    calls_after_first = len(provider.bar_calls)

    update(cfg, provider=provider, as_of=last)
    assert len(provider.bar_calls) == calls_after_first


# ---------------------------------------------------------------------------
# provider identity
# ---------------------------------------------------------------------------
def test_a_fresh_cache_adopts_the_configured_provider(base_config):
    from swing.data.pipeline import ensure_provider

    cache = BarCache(base_config.expand_path(base_config.data.cache_dir))
    assert cache.stamped_provider() is None
    assert ensure_provider(base_config, cache) == "yfinance"
    assert cache.stamped_provider() == "yfinance"


def test_an_unstamped_existing_cache_is_migrated_not_rejected(base_config):
    """Caches written before stamping existed must keep working.

    Whatever is in them was written by whatever was configured then, which is
    what the user is running now — so adopting the stamp is safe, and refusing
    would strand every existing install behind a 45-minute re-download.
    """
    from swing.data.pipeline import ensure_provider

    cache = BarCache(base_config.expand_path(base_config.data.cache_dir))
    cache.write("AAA", trending_bars(n=30))
    assert cache.stamped_provider() is None

    ensure_provider(base_config, cache)
    assert cache.stamped_provider() == "yfinance"
    assert len(cache.read("AAA")) == 30      # nothing was discarded


def test_switching_providers_on_a_stamped_cache_is_refused(base_config):
    """The whole point: two adjustment bases must never be spliced together."""
    from swing.data.cache import ProviderMismatch
    from swing.data.pipeline import ensure_provider

    cache = BarCache(base_config.expand_path(base_config.data.cache_dir))
    cache.stamp_provider("yfinance")

    data = base_config.as_dict()
    data["data"]["provider"] = "stooq"
    switched = Config(data)

    with pytest.raises(ProviderMismatch) as excinfo:
        ensure_provider(switched, cache)

    message = str(excinfo.value)
    assert "yfinance" in message and "stooq" in message
    assert "backfill" in message              # tells you how to recover
    assert str(cache.root) in message         # names the cache to delete


def test_backfill_refuses_to_write_into_a_mismatched_cache(base_config):
    """A refusal that still downloaded would defeat the purpose."""
    from swing.data.cache import ProviderMismatch

    cache = BarCache(base_config.expand_path(base_config.data.cache_dir))
    cache.stamp_provider("stooq")

    provider = FakeProvider(bars={s: trending_bars(n=100) for s in ("AAA", "BBB", "SPY")})
    with pytest.raises(ProviderMismatch):
        backfill(base_config, provider=provider)

    assert provider.bar_calls == []           # nothing was fetched
    assert cache.symbols() == []              # nothing was written


def test_update_refuses_on_a_mismatched_cache(base_config):
    from swing.data.cache import ProviderMismatch

    cache = BarCache(base_config.expand_path(base_config.data.cache_dir))
    cache.stamp_provider("schwab")
    provider = FakeProvider(bars={"AAA": trending_bars(n=100)})

    with pytest.raises(ProviderMismatch):
        update(base_config, provider=provider, as_of=date(2021, 1, 1))
    assert provider.bar_calls == []


def test_matching_provider_proceeds_normally(base_config):
    cache = BarCache(base_config.expand_path(base_config.data.cache_dir))
    cache.stamp_provider("yfinance")
    provider = FakeProvider(bars={s: trending_bars(n=100) for s in ("AAA", "BBB", "SPY")})

    backfill(base_config, provider=provider)
    assert cache.has("AAA")
    assert cache.stamped_provider() == "yfinance"


def test_the_stamp_survives_other_cache_writes(base_config):
    """meta.json must not be clobbered by earnings/fundamentals/absent writes."""
    from swing.data.provider import Fundamentals

    cache = BarCache(base_config.expand_path(base_config.data.cache_dir))
    cache.stamp_provider("stooq")
    cache.write("AAA", trending_bars(n=30))
    cache.write_earnings({"AAA": None})
    cache.write_fundamentals({"AAA": Fundamentals(symbol="AAA")})
    cache.mark_absent(["ZZZ"])
    assert cache.stamped_provider() == "stooq"


def test_cache_status_reports_the_provider_and_flags_a_mismatch(base_config):
    from swing.data.pipeline import cache_status

    cache = BarCache(base_config.expand_path(base_config.data.cache_dir))
    cache.write("AAA", trending_bars(n=30))
    cache.stamp_provider("yfinance")
    assert "yfinance" in cache_status(base_config)

    data = base_config.as_dict()
    data["data"]["provider"] = "stooq"
    assert "MISMATCH" in cache_status(Config(data))
