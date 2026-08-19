"""Tests for the parquet bar cache and the TTL sidecar caches.

The behaviours pinned here are the ones that cost real money if they break:

* a warm cache must do **zero** network calls,
* a tail fetch must ask for the missing dates only,
* and a vendor that re-adjusts history (dividend, split) must trigger a full
  refetch instead of leaving a step discontinuity mid-series.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from conftest import make_bars
from swing.data.cache import BarCache, CacheMeta, TtlJsonCache

NOW = datetime(2026, 8, 18, 21, 0, tzinfo=UTC)
FULL = make_bars(60, start="2020-01-02")
FIRST_DAY = FULL.index[0].date()
LAST_DAY = FULL.index[-1].date()
MID_DAY = FULL.index[29].date()


class RecordingFetch:
    """A stand-in vendor: serves slices of ``source`` and records every call."""

    def __init__(self, source: pd.DataFrame | None = None) -> None:
        self.source = FULL if source is None else source
        self.calls: list[tuple[list[str], date, date]] = []

    def __call__(self, symbols, start: date, end: date) -> dict[str, pd.DataFrame]:
        self.calls.append((list(symbols), start, end))
        window = self.source.loc[str(start) : str(end)]
        return {symbol: window.copy() for symbol in symbols}

    @property
    def count(self) -> int:
        return len(self.calls)


@pytest.fixture
def cache(tmp_path: Path) -> BarCache:
    return BarCache(tmp_path / "cache" / "daily")


# ---------------------------------------------------------------------------
# cold / warm
# ---------------------------------------------------------------------------


def test_cold_miss_fetches_once_and_writes_parquet_and_meta(cache: BarCache) -> None:
    fetch = RecordingFetch()
    out = cache.get_bars(["AAPL", "MSFT"], FIRST_DAY, LAST_DAY, fetch, now=NOW)

    assert fetch.count == 1, "both symbols share one batched call"
    assert fetch.calls[0] == (["AAPL", "MSFT"], FIRST_DAY, LAST_DAY)
    assert sorted(out) == ["AAPL", "MSFT"]
    assert cache.path_for("AAPL").is_file()

    meta = cache.read_meta("AAPL")
    assert meta is not None
    assert (meta.covered_start, meta.covered_end) == (FIRST_DAY, LAST_DAY)
    assert (meta.first_bar, meta.last_bar) == (FIRST_DAY, LAST_DAY)
    assert meta.rows == len(FULL)
    assert meta.fetched_at == NOW


def test_warm_hit_never_touches_the_vendor(cache: BarCache) -> None:
    fetch = RecordingFetch()
    cache.get_bars(["AAPL"], FIRST_DAY, LAST_DAY, fetch, now=NOW)
    fetch.calls.clear()

    out = cache.get_bars(["AAPL"], FIRST_DAY, LAST_DAY, fetch, now=NOW)

    assert fetch.count == 0
    pd.testing.assert_frame_equal(out["AAPL"], FULL)


def test_a_narrower_warm_request_is_sliced_not_refetched(cache: BarCache) -> None:
    fetch = RecordingFetch()
    cache.get_bars(["AAPL"], FIRST_DAY, LAST_DAY, fetch, now=NOW)
    fetch.calls.clear()

    out = cache.get_bars(["AAPL"], MID_DAY, LAST_DAY, fetch, now=NOW)

    assert fetch.count == 0
    assert out["AAPL"].index[0].date() == MID_DAY
    assert out["AAPL"].index[-1].date() == LAST_DAY


def test_cached_bars_keep_the_contract_shape_across_a_parquet_round_trip(
    cache: BarCache,
) -> None:
    cache.get_bars(["AAPL"], FIRST_DAY, LAST_DAY, RecordingFetch(), now=NOW)
    reread = cache.read("AAPL")
    assert reread is not None
    assert reread.index.name is None
    assert list(reread.columns) == ["open", "high", "low", "close", "volume"]
    assert all(dtype == "float64" for dtype in reread.dtypes)
    pd.testing.assert_frame_equal(reread, FULL)


# ---------------------------------------------------------------------------
# incremental tail
# ---------------------------------------------------------------------------


def test_tail_fetch_asks_only_for_the_missing_dates_plus_an_overlap(cache: BarCache) -> None:
    first_leg = FULL.loc[: str(MID_DAY)]
    fetch = RecordingFetch(source=first_leg)
    cache.get_bars(["AAPL"], FIRST_DAY, MID_DAY, fetch, now=NOW)

    fetch.source = FULL
    fetch.calls.clear()
    out = cache.get_bars(["AAPL"], FIRST_DAY, LAST_DAY, fetch, now=NOW)

    assert fetch.count == 1
    symbols, start, end = fetch.calls[0]
    assert symbols == ["AAPL"]
    assert start == first_leg.index[-5].date(), "the last five cached bars are re-requested"
    assert end == LAST_DAY
    pd.testing.assert_frame_equal(out["AAPL"], FULL)


def test_tail_fetch_batches_symbols_that_share_a_cache_edge(cache: BarCache) -> None:
    fetch = RecordingFetch(source=FULL.loc[: str(MID_DAY)])
    cache.get_bars(["AAPL", "MSFT"], FIRST_DAY, MID_DAY, fetch, now=NOW)

    fetch.source = FULL
    fetch.calls.clear()
    cache.get_bars(["AAPL", "MSFT"], FIRST_DAY, LAST_DAY, fetch, now=NOW)

    assert fetch.count == 1
    assert fetch.calls[0][0] == ["AAPL", "MSFT"]


def test_a_missing_head_refetches_the_whole_range(cache: BarCache) -> None:
    fetch = RecordingFetch()
    cache.get_bars(["AAPL"], MID_DAY, LAST_DAY, fetch, now=NOW)
    fetch.calls.clear()

    cache.get_bars(["AAPL"], FIRST_DAY, LAST_DAY, fetch, now=NOW)

    assert fetch.count == 1
    assert fetch.calls[0][1] == FIRST_DAY, "an earlier start means the tail trick cannot help"


def test_an_empty_tail_is_remembered_so_a_weekend_rerun_stays_offline(cache: BarCache) -> None:
    """Nothing traded after the last cached bar: mark it covered, do not re-ask."""
    fetch = RecordingFetch()
    cache.get_bars(["AAPL"], FIRST_DAY, LAST_DAY, fetch, now=NOW)

    later = LAST_DAY + timedelta(days=2)
    fetch.calls.clear()
    cache.get_bars(["AAPL"], FIRST_DAY, later, fetch, now=NOW)
    assert fetch.count == 1

    fetch.calls.clear()
    out = cache.get_bars(["AAPL"], FIRST_DAY, later, fetch, now=NOW)
    assert fetch.count == 0
    assert len(out["AAPL"]) == len(FULL)


# ---------------------------------------------------------------------------
# the overlap check — the reason this cache is not a plain append
# ---------------------------------------------------------------------------


def _readjusted(frame: pd.DataFrame, factor: float) -> pd.DataFrame:
    """Simulate a dividend re-adjustment: every historical price scaled down."""
    out = frame.copy()
    for column in ("open", "high", "low", "close"):
        out.loc[:, column] = out[column] * factor
    return out


def test_overlap_mismatch_triggers_a_full_refetch(cache: BarCache) -> None:
    first_leg = FULL.loc[: str(MID_DAY)]
    fetch = RecordingFetch(source=first_leg)
    cache.get_bars(["AAPL"], FIRST_DAY, MID_DAY, fetch, now=NOW)

    # The vendor has since re-adjusted every close by -1% (a dividend went ex).
    adjusted = _readjusted(FULL, 0.99)
    fetch.source = adjusted
    fetch.calls.clear()
    out = cache.get_bars(["AAPL"], FIRST_DAY, LAST_DAY, fetch, now=NOW)

    assert fetch.count == 2, "a tail probe, then a full refetch"
    assert fetch.calls[1][1] == FIRST_DAY, "the refetch starts at the beginning of history"
    pd.testing.assert_frame_equal(out["AAPL"], adjusted)
    pd.testing.assert_frame_equal(cache.read("AAPL"), adjusted)


def test_a_refetch_preserves_history_older_than_this_request(cache: BarCache) -> None:
    """A re-adjustment must not silently truncate the cache to the asked-for window."""
    first_leg = FULL.loc[: str(MID_DAY)]
    fetch = RecordingFetch(source=first_leg)
    cache.get_bars(["AAPL"], FIRST_DAY, MID_DAY, fetch, now=NOW)

    fetch.source = _readjusted(FULL, 0.99)
    fetch.calls.clear()
    later_start = FULL.index[10].date()
    cache.get_bars(["AAPL"], later_start, LAST_DAY, fetch, now=NOW)

    assert fetch.calls[-1][1] == FIRST_DAY
    assert cache.read("AAPL").index[0].date() == FIRST_DAY


def test_a_difference_inside_the_tolerance_is_treated_as_the_same_series(
    cache: BarCache,
) -> None:
    first_leg = FULL.loc[: str(MID_DAY)]
    fetch = RecordingFetch(source=first_leg)
    cache.get_bars(["AAPL"], FIRST_DAY, MID_DAY, fetch, now=NOW)

    fetch.source = _readjusted(FULL, 1.0000005)  # rounding noise, not a re-adjustment
    fetch.calls.clear()
    cache.get_bars(["AAPL"], FIRST_DAY, LAST_DAY, fetch, now=NOW)

    assert fetch.count == 1, "no refetch for rounding-level differences"


def test_the_tolerance_boundary_is_where_the_docstring_says_it_is(cache: BarCache) -> None:
    base = FULL.iloc[:10]
    fresh_ok = _readjusted(base, 1.0005)  # 0.05% — inside 0.1%
    fresh_bad = _readjusted(base, 1.002)  # 0.2% — outside
    assert cache._overlap_conflicts(base, fresh_ok) is False
    assert cache._overlap_conflicts(base, fresh_bad) is True


def test_overlap_check_ignores_frames_that_do_not_share_a_date(cache: BarCache) -> None:
    assert cache._overlap_conflicts(FULL.iloc[:5], FULL.iloc[10:15]) is False
    assert cache._overlap_conflicts(FULL.iloc[:5], FULL.iloc[:0]) is False


# ---------------------------------------------------------------------------
# damage control
# ---------------------------------------------------------------------------


def test_a_corrupt_parquet_is_reported_removed_and_refetched(cache: BarCache) -> None:
    fetch = RecordingFetch()
    cache.get_bars(["AAPL"], FIRST_DAY, LAST_DAY, fetch, now=NOW)
    cache.path_for("AAPL").write_bytes(b"this is not a parquet file")
    fetch.calls.clear()

    with pytest.warns(UserWarning, match="could not be read"):
        out = cache.get_bars(["AAPL"], FIRST_DAY, LAST_DAY, fetch, now=NOW)

    assert fetch.count == 1
    pd.testing.assert_frame_equal(out["AAPL"], FULL)
    assert cache.read("AAPL") is not None


def test_a_lost_sidecar_is_rebuilt_from_the_parquet_instead_of_refetching(
    cache: BarCache,
) -> None:
    fetch = RecordingFetch()
    cache.get_bars(["AAPL"], FIRST_DAY, LAST_DAY, fetch, now=NOW)
    cache.meta_path_for("AAPL").unlink()
    fetch.calls.clear()

    out = cache.get_bars(["AAPL"], FIRST_DAY, LAST_DAY, fetch, now=NOW)

    assert fetch.count == 0
    assert len(out["AAPL"]) == len(FULL)


def test_a_garbled_sidecar_is_ignored_rather_than_fatal(cache: BarCache) -> None:
    fetch = RecordingFetch()
    cache.get_bars(["AAPL"], FIRST_DAY, LAST_DAY, fetch, now=NOW)
    cache.meta_path_for("AAPL").write_text("{not json", encoding="utf-8")

    assert cache.read_meta("AAPL") is None
    assert len(cache.get_bars(["AAPL"], FIRST_DAY, LAST_DAY, fetch, now=NOW)["AAPL"]) == len(FULL)


def test_a_vendor_failure_serves_cached_data_instead_of_raising(cache: BarCache) -> None:
    fetch = RecordingFetch()
    cache.get_bars(["AAPL"], FIRST_DAY, MID_DAY, fetch, now=NOW)

    def broken(symbols, start, end):
        raise RuntimeError("Yahoo is having a day")

    out = cache.get_bars(["AAPL"], FIRST_DAY, LAST_DAY, broken, now=NOW)
    assert out["AAPL"].index[-1].date() == MID_DAY


def test_a_cold_vendor_failure_simply_omits_the_symbol(cache: BarCache) -> None:
    def broken(symbols, start, end):
        raise RuntimeError("Yahoo is having a day")

    assert cache.get_bars(["AAPL"], FIRST_DAY, LAST_DAY, broken, now=NOW) == {}
    assert not cache.path_for("AAPL").is_file()


def test_a_symbol_the_vendor_drops_is_omitted_not_cached_empty(cache: BarCache) -> None:
    def partial(symbols, start, end):
        return {"AAPL": FULL.copy()}  # MSFT is silently missing, as Yahoo does

    out = cache.get_bars(["AAPL", "MSFT"], FIRST_DAY, LAST_DAY, partial, now=NOW)
    assert sorted(out) == ["AAPL"]
    assert not cache.path_for("MSFT").is_file()


def test_symbols_with_awkward_characters_get_safe_filenames(cache: BarCache) -> None:
    cache.get_bars(["BRK-B", "BF.B"], FIRST_DAY, LAST_DAY, RecordingFetch(), now=NOW)
    assert cache.path_for("BRK-B").name == "BRK-B.parquet"
    assert cache.path_for("BF.B").name == "BF.B.parquet"
    assert cache.path_for("A/B").name == "A_B.parquet"


def test_backwards_date_ranges_are_refused_in_plain_english(cache: BarCache) -> None:
    with pytest.raises(ValueError, match="after it ends"):
        cache.get_bars(["AAPL"], LAST_DAY, FIRST_DAY, RecordingFetch(), now=NOW)


def test_cache_construction_rejects_a_useless_overlap(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="overlap_rows"):
        BarCache(tmp_path, overlap_rows=0)
    with pytest.raises(ValueError, match="tolerance"):
        BarCache(tmp_path, tolerance=-1.0)


def test_cache_meta_round_trips_through_json() -> None:
    meta = CacheMeta(
        symbol="AAPL",
        covered_start=date(2020, 1, 1),
        covered_end=date(2026, 8, 18),
        first_bar=date(2020, 1, 2),
        last_bar=date(2026, 8, 17),
        rows=1650,
        fetched_at=NOW,
    )
    assert CacheMeta.from_json(json.loads(json.dumps(meta.to_json()))) == meta
    assert CacheMeta.from_json({"symbol": "AAPL"}) is None
    assert CacheMeta.from_json("nonsense") is None


# ---------------------------------------------------------------------------
# TtlJsonCache
# ---------------------------------------------------------------------------


@pytest.fixture
def ttl_cache(tmp_path: Path) -> TtlJsonCache:
    return TtlJsonCache(tmp_path / "earnings.json", timedelta(days=3))


def test_ttl_cache_fetches_once_then_serves_from_disk(ttl_cache: TtlJsonCache) -> None:
    calls: list[list[str]] = []

    def fetch(keys: list[str]) -> dict[str, str]:
        calls.append(keys)
        return dict.fromkeys(keys, "2026-09-01")

    assert ttl_cache.get_or_fetch(["AAPL"], fetch, now=NOW) == {"AAPL": "2026-09-01"}
    assert ttl_cache.get_or_fetch(["AAPL"], fetch, now=NOW + timedelta(days=2)) == {
        "AAPL": "2026-09-01"
    }
    assert calls == [["AAPL"]]


def test_ttl_cache_refetches_once_the_value_is_stale(ttl_cache: TtlJsonCache) -> None:
    calls: list[list[str]] = []

    def fetch(keys: list[str]) -> dict[str, str]:
        calls.append(keys)
        return dict.fromkeys(keys, f"call-{len(calls)}")

    ttl_cache.get_or_fetch(["AAPL"], fetch, now=NOW)
    fresh = ttl_cache.get_or_fetch(["AAPL"], fetch, now=NOW + timedelta(days=3, seconds=1))
    assert fresh == {"AAPL": "call-2"}
    assert len(calls) == 2


def test_ttl_cache_only_asks_for_the_keys_it_is_missing(ttl_cache: TtlJsonCache) -> None:
    calls: list[list[str]] = []

    def fetch(keys: list[str]) -> dict[str, str]:
        calls.append(keys)
        return dict.fromkeys(keys, "x")

    ttl_cache.get_or_fetch(["AAPL"], fetch, now=NOW)
    out = ttl_cache.get_or_fetch(["AAPL", "MSFT"], fetch, now=NOW)
    assert sorted(out) == ["AAPL", "MSFT"]
    assert calls == [["AAPL"], ["MSFT"]]


def test_ttl_cache_remembers_a_negative_answer(ttl_cache: TtlJsonCache) -> None:
    calls: list[list[str]] = []

    def fetch(keys: list[str]) -> dict[str, None]:
        calls.append(keys)
        return dict.fromkeys(keys)

    assert ttl_cache.get_or_fetch(["AAPL"], fetch, now=NOW) == {"AAPL": None}
    assert ttl_cache.get_or_fetch(["AAPL"], fetch, now=NOW) == {"AAPL": None}
    assert len(calls) == 1, "'no earnings date' is an answer worth caching"


def test_ttl_cache_encodes_and_decodes_values(ttl_cache: TtlJsonCache) -> None:
    encode = lambda value: value.isoformat()  # noqa: E731
    decode = lambda value: date.fromisoformat(value)  # noqa: E731
    day = date(2026, 9, 1)

    ttl_cache.get_or_fetch(["AAPL"], lambda keys: {"AAPL": day}, now=NOW, encode=encode)
    out = ttl_cache.get_or_fetch(["AAPL"], lambda keys: {}, now=NOW, decode=decode)
    assert out == {"AAPL": day}


def test_ttl_cache_recovers_from_a_corrupt_file(ttl_cache: TtlJsonCache) -> None:
    ttl_cache.path.write_text("{ this is not json", encoding="utf-8")
    with pytest.warns(UserWarning, match="could not be read"):
        out = ttl_cache.get_or_fetch(["AAPL"], lambda keys: {"AAPL": 1}, now=NOW)
    assert out == {"AAPL": 1}
    assert ttl_cache.read_all()["AAPL"]["value"] == 1


def test_ttl_cache_survives_a_failing_fetch(ttl_cache: TtlJsonCache) -> None:
    def broken(keys: list[str]) -> dict[str, str]:
        raise RuntimeError("Yahoo said no")

    assert ttl_cache.get_or_fetch(["AAPL"], broken, now=NOW) == {}


def test_ttl_cache_rejects_a_nonsense_lifetime(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="positive amount of time"):
        TtlJsonCache(tmp_path / "x.json", timedelta(0))
