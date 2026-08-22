"""Tests for the parquet bar cache and the TTL sidecar caches.

The behaviours pinned here are the ones that cost real money if they break:

* a warm cache must do **zero** network calls,
* a tail fetch must ask for the missing dates only,
* and a vendor that re-adjusts history (dividend, split) must trigger a full
  refetch instead of leaving a step discontinuity mid-series.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from conftest import make_bars
from swing.data.cache import (
    BarCache,
    CacheMeta,
    TtlJsonCache,
    earnings_coverage,
    earnings_fingerprint,
)
from swing.state import file_lock

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


def warm(cache: BarCache, through: date = LAST_DAY, *, source: pd.DataFrame = FULL) -> None:
    """Fill the cache for AAPL from the first day up to ``through``."""
    cache.get_bars(["AAPL"], FIRST_DAY, through, RecordingFetch(source=source), now=NOW)


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


def test_a_tail_with_no_new_bars_is_remembered_so_the_rerun_stays_offline(
    cache: BarCache,
) -> None:
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


def test_an_empty_answer_over_a_weekend_is_recorded_as_covered(cache: BarCache) -> None:
    """The market was shut, so a rerun on Sunday must stay offline."""
    friday = date(2020, 3, 20)
    assert friday.weekday() == 4
    leg = FULL.loc[: str(friday)]
    warm(cache, friday, source=leg)
    sunday = friday + timedelta(days=2)

    calls: list[tuple[date, date]] = []

    def nothing_traded(symbols, start, end):
        calls.append((start, end))
        return {symbol: FULL.iloc[:0].copy() for symbol in symbols}

    cache.get_bars(["AAPL"], FIRST_DAY, sunday, nothing_traded, now=NOW)
    assert len(calls) == 1
    assert cache.read_meta("AAPL").covered_end == sunday

    cache.get_bars(["AAPL"], FIRST_DAY, sunday, nothing_traded, now=NOW)
    assert len(calls) == 1, "the weekend is genuinely covered"


def test_an_empty_answer_on_a_trading_day_is_not_recorded_as_covered(
    cache: BarCache,
) -> None:
    """Audit BUG-035: "nothing traded" and "the vendor blinked" look identical.

    A transient failure at 21:00 used to mark the symbol covered through today,
    so the natural rerun an hour later stayed offline on stale bars — and the
    empty frame comes back from a *successful* call, so no exception, no log,
    nothing to notice.
    """
    warm(cache, MID_DAY, source=FULL.loc[: str(MID_DAY)])
    wednesday = MID_DAY + timedelta(days=1)
    assert wednesday.weekday() < 5

    calls: list[tuple[date, date]] = []

    def blinked(symbols, start, end):
        calls.append((start, end))
        return {symbol: FULL.iloc[:0].copy() for symbol in symbols}

    cache.get_bars(["AAPL"], FIRST_DAY, wednesday, blinked, now=NOW)
    assert cache.read_meta("AAPL").covered_end == MID_DAY, "coverage does not move"

    recovered = RecordingFetch()
    out = cache.get_bars(["AAPL"], FIRST_DAY, wednesday, recovered, now=NOW)

    assert recovered.count == 1, "the rerun an hour later actually asks again"
    assert out["AAPL"].index[-1].date() > MID_DAY


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


def test_a_uniform_shift_is_a_re_adjustment_however_small_it_is(cache: BarCache) -> None:
    """Audit BUG-014a: a 0.05% dividend used to be *merged* under the tolerance.

    That is the exact step discontinuity this cache exists to prevent — old
    basis 87.317 sitting beside new basis 83.742 — and low-yield names
    accumulated it a couple of tenths of a percent a year. Uniformity, not
    magnitude, is what identifies a re-adjustment: every bar moves by the same
    factor.
    """
    base = FULL.iloc[:10]
    assert cache._overlap_conflicts(base, _readjusted(base, 1.0005)) is True
    assert cache._overlap_conflicts(base, _readjusted(base, 1.002)) is True


def test_scattered_rounding_noise_is_not_a_re_adjustment(cache: BarCache) -> None:
    """The other half of BUG-014a: per-bar noise must still append, not refetch."""
    base = FULL.iloc[:10]
    noisy = base.copy()
    scale = [1.0 + 0.00005 * (-1) ** i for i in range(len(noisy))]
    noisy.loc[:, "close"] = noisy["close"].to_numpy() * scale
    assert cache._overlap_conflicts(base, noisy) is False


def test_a_sub_tolerance_re_adjustment_forces_a_full_refetch(cache: BarCache) -> None:
    """BUG-014a end to end: the splice must never reach the parquet."""
    warm(cache, MID_DAY, source=FULL.loc[: str(MID_DAY)])
    adjusted = _readjusted(FULL, 1.0005)  # 0.05% — under the old 0.1% tolerance
    fetch = RecordingFetch(source=adjusted)

    out = cache.get_bars(["AAPL"], FIRST_DAY, LAST_DAY, fetch, now=NOW)

    assert fetch.count == 2, "a tail probe, then a full refetch on the new basis"
    assert fetch.calls[1][1] == FIRST_DAY
    pd.testing.assert_frame_equal(out["AAPL"], adjusted)


def test_a_tail_response_sharing_no_dates_is_treated_as_a_conflict(cache: BarCache) -> None:
    """Audit BUG-014b: a tail fetch starts *at* a cached bar by construction.

    Zero shared dates therefore means the vendor ignored the range we asked
    for — precisely when its basis is least trustworthy — and the old code
    read that as "no conflict" and concatenated the two halves.
    """
    warm(cache, MID_DAY, source=FULL.loc[: str(MID_DAY)])
    disjoint = FULL.loc[str(FULL.index[35].date()) :]
    calls: list[tuple[date, date]] = []

    def fetch(symbols, start, end):
        calls.append((start, end))
        if len(calls) == 1:
            return {symbol: disjoint.copy() for symbol in symbols}  # the range is ignored
        return {symbol: FULL.loc[str(start) : str(end)].copy() for symbol in symbols}

    out = cache.get_bars(["AAPL"], FIRST_DAY, LAST_DAY, fetch, now=NOW)

    assert len(calls) == 2, "the disjoint answer is refused, then history is refetched"
    assert calls[1][0] == FIRST_DAY
    assert out["AAPL"].index.is_unique
    pd.testing.assert_frame_equal(out["AAPL"], FULL)


def test_history_older_than_the_refetch_window_is_downloaded_again(cache: BarCache) -> None:
    """Audit BUG-014c: small re-adjustments accumulate and nothing else resets them."""
    warm(cache)
    fetch = RecordingFetch()

    assert cache.get_bars(["AAPL"], FIRST_DAY, LAST_DAY, fetch, now=NOW + timedelta(days=89))
    assert fetch.count == 0, "inside the window a complete cache is still free"

    cache.get_bars(["AAPL"], FIRST_DAY, LAST_DAY, fetch, now=NOW + timedelta(days=91))
    assert fetch.count == 1
    assert fetch.calls[0][1] == FIRST_DAY, "the whole history, not just a tail"


def test_overlap_check_ignores_frames_that_do_not_share_a_date(cache: BarCache) -> None:
    assert cache._overlap_conflicts(FULL.iloc[:5], FULL.iloc[10:15]) is False
    assert cache._overlap_conflicts(FULL.iloc[:5], FULL.iloc[:0]) is False


# ---------------------------------------------------------------------------
# damage control
# ---------------------------------------------------------------------------


def test_a_corrupt_parquet_is_reported_removed_and_refetched(
    cache: BarCache, caplog: pytest.LogCaptureFixture
) -> None:
    """The report is a log line, not ``warnings.warn`` (audit DEBT-014).

    This runs once per symbol inside a 1,500-iteration loop, where every
    sibling failure already logs.
    """
    fetch = RecordingFetch()
    cache.get_bars(["AAPL"], FIRST_DAY, LAST_DAY, fetch, now=NOW)
    cache.path_for("AAPL").write_bytes(b"this is not a parquet file")
    fetch.calls.clear()

    with caplog.at_level("WARNING", logger="swing.data.cache"):
        out = cache.get_bars(["AAPL"], FIRST_DAY, LAST_DAY, fetch, now=NOW)

    assert "could not be read" in caplog.text
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


# ---------------------------------------------------------------------------
# the sidecar must describe the file beside it (audit BUG-004)
# ---------------------------------------------------------------------------


def _rewrite_meta(cache: BarCache, symbol: str, **changes) -> None:
    """Hand-edit a sidecar the way a racing second writer would leave it."""
    raw = json.loads(cache.meta_path_for(symbol).read_text(encoding="utf-8"))
    raw.update(changes)
    cache.meta_path_for(symbol).write_text(json.dumps(raw), encoding="utf-8")


def test_a_sidecar_that_does_not_match_its_parquet_is_rebuilt_and_healed(
    cache: BarCache, caplog: pytest.LogCaptureFixture
) -> None:
    """Audit BUG-004: 150 rows served as "fully cached, zero network".

    The parquet and the sidecar are two atomic writes, not one, so a second
    process can leave a record describing a different frame. Believing it is
    how SMA-200 and ATR end up computed on a series that stops months early —
    with no error, no warning and no self-healing.
    """
    warm(cache, MID_DAY, source=FULL.loc[: str(MID_DAY)])
    _rewrite_meta(cache, "AAPL", covered_end=LAST_DAY.isoformat(), rows=len(FULL))
    fetch = RecordingFetch()

    with caplog.at_level("WARNING", logger="swing.data.cache"):
        out = cache.get_bars(["AAPL"], FIRST_DAY, LAST_DAY, fetch, now=NOW)

    assert "is being rebuilt from the file" in caplog.text
    assert fetch.count == 1, "the truncated tail is noticed and downloaded"
    assert len(out["AAPL"]) == len(FULL)
    assert cache.read_meta("AAPL").rows == len(FULL)


def test_a_sidecar_claiming_the_wrong_last_bar_is_rebuilt(cache: BarCache) -> None:
    warm(cache, MID_DAY, source=FULL.loc[: str(MID_DAY)])
    _rewrite_meta(cache, "AAPL", covered_end=LAST_DAY.isoformat(), last_bar=LAST_DAY.isoformat())

    fetch = RecordingFetch()
    cache.get_bars(["AAPL"], FIRST_DAY, LAST_DAY, fetch, now=NOW)

    assert fetch.count == 1


def test_the_mutating_half_of_a_fetch_holds_the_cache_lock(cache: BarCache) -> None:
    """Audit BUG-004: two writers must not interleave on one cache directory."""
    held: list[bool] = []

    def fetch(symbols, start, end):
        try:
            with file_lock(cache.lock_path, timeout=0):
                held.append(False)
        except TimeoutError:
            held.append(True)
        return {symbol: FULL.copy() for symbol in symbols}

    cache.get_bars(["AAPL"], FIRST_DAY, LAST_DAY, fetch, now=NOW)

    assert held == [True], "the lock is already held while the vendor call runs"


def test_a_warm_read_takes_no_lock_at_all(cache: BarCache) -> None:
    """Zero network *and* zero contention: a warm scan must not block a backtest."""
    warm(cache)
    lock_file = cache.lock_path.with_name(cache.lock_path.name + ".lock")
    lock_file.unlink(missing_ok=True)

    cache.get_bars(["AAPL"], FIRST_DAY, LAST_DAY, RecordingFetch(), now=NOW)

    assert not lock_file.exists()


def test_a_cache_locked_by_another_process_degrades_to_what_is_on_disk(
    cache: BarCache, caplog: pytest.LogCaptureFixture
) -> None:
    warm(cache, MID_DAY, source=FULL.loc[: str(MID_DAY)])
    fetch = RecordingFetch()
    cache.lock_timeout = 0.0

    with file_lock(cache.lock_path, timeout=0), caplog.at_level("WARNING"):
        out = cache.get_bars(["AAPL"], FIRST_DAY, LAST_DAY, fetch, now=NOW)

    assert fetch.count == 0
    assert "still writing the price cache" in caplog.text
    assert out["AAPL"].index[-1].date() == MID_DAY, "stale beats nothing, and it says so"


# ---------------------------------------------------------------------------
# coverage is a range, and a refetch widens it (audit BUG-013, BUG-050)
# ---------------------------------------------------------------------------


def test_a_refetch_for_an_earlier_end_keeps_the_live_tail(cache: BarCache) -> None:
    """Audit BUG-013: 49 live-tail bars deleted by ``swing backtest --end <past>``.

    Both refetch paths asked for ``[from_date, end]`` and *replaced* the file.
    The start side was widened to the cached start; the end side was not, and
    ``covered_end`` was not even tracked.
    """
    cache.get_bars(["AAPL"], MID_DAY, LAST_DAY, RecordingFetch(), now=NOW)
    fetch = RecordingFetch()

    cache.get_bars(["AAPL"], FIRST_DAY, MID_DAY, fetch, now=NOW)

    assert fetch.count == 1
    assert fetch.calls[0][1] == FIRST_DAY, "the missing head"
    assert fetch.calls[0][2] == LAST_DAY, "and the tail we already had — the union"
    assert cache.read("AAPL").index[-1].date() == LAST_DAY
    meta = cache.read_meta("AAPL")
    assert (meta.covered_start, meta.covered_end) == (FIRST_DAY, LAST_DAY)


def test_a_conflict_refetch_also_keeps_the_live_tail(cache: BarCache) -> None:
    """The same union rule on the re-adjustment path (audit BUG-013)."""
    warm(cache)
    earlier_end = FULL.index[45].date()
    fetch = RecordingFetch(source=_readjusted(FULL, 0.97))

    cache.get_bars(["AAPL"], FIRST_DAY, earlier_end, fetch, now=NOW + timedelta(days=91))

    assert fetch.calls[-1][2] == LAST_DAY
    assert cache.read("AAPL").index[-1].date() == LAST_DAY


def test_a_lost_sidecar_does_not_turn_a_late_listing_into_a_full_refetch(
    cache: BarCache,
) -> None:
    """Audit BUG-050: a symbol that listed in 2020 has no 2019 bars to fetch.

    Rebuilding coverage from the frame's own first bar made every run see a
    missing head and refetch the lot.
    """
    warm(cache)
    cache.meta_path_for("AAPL").unlink()
    fetch = RecordingFetch()

    out = cache.get_bars(["AAPL"], FIRST_DAY - timedelta(days=400), LAST_DAY, fetch, now=NOW)

    assert fetch.count == 0
    assert len(out["AAPL"]) == len(FULL)


# ---------------------------------------------------------------------------
# a failed download is not "nothing traded" (audit BUG-035)
# ---------------------------------------------------------------------------


def test_a_failed_tail_fetch_does_not_mark_the_symbol_covered(cache: BarCache) -> None:
    """The other half of BUG-035, which already held: a raised call is not coverage.

    ``_call`` now returns a sentinel rather than an empty dict, so "the batch
    failed" and "the batch answered with nothing" are different outcomes to
    every caller — this pins the behaviour the sentinel must preserve.
    """
    warm(cache, MID_DAY, source=FULL.loc[: str(MID_DAY)])

    def broken(symbols, start, end):
        raise RuntimeError("Yahoo is having a day")

    cache.get_bars(["AAPL"], FIRST_DAY, LAST_DAY, broken, now=NOW)
    assert cache.read_meta("AAPL").covered_end == MID_DAY, "coverage stands still"

    fetch = RecordingFetch()
    out = cache.get_bars(["AAPL"], FIRST_DAY, LAST_DAY, fetch, now=NOW)

    assert fetch.count == 1, "the rerun an hour later actually tries again"
    assert len(out["AAPL"]) == len(FULL)


def test_a_symbol_the_vendor_drops_from_a_good_batch_keeps_its_old_coverage(
    cache: BarCache,
) -> None:
    warm(cache, MID_DAY, source=FULL.loc[: str(MID_DAY)])

    def partial(symbols, start, end):
        return {}  # the call worked; this symbol simply was not in the answer

    cache.get_bars(["AAPL"], FIRST_DAY, LAST_DAY, partial, now=NOW)
    assert cache.read_meta("AAPL").covered_end == MID_DAY


# ---------------------------------------------------------------------------
# housekeeping: the read memo (PERF-006) and abandoned temp files (LEAK-002)
# ---------------------------------------------------------------------------


def test_an_unchanged_parquet_is_read_from_disk_only_once(
    cache: BarCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audit PERF-006: every warm scan re-read and re-normalised every file."""
    warm(cache)
    reads: list[Path] = []
    real = pd.read_parquet

    def counting(path, *args, **kwargs):
        reads.append(Path(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(pd, "read_parquet", counting)
    reader = BarCache(cache.root)

    first = reader.read("AAPL")
    second = reader.read("AAPL")
    third = reader.get_bars(["AAPL"], FIRST_DAY, LAST_DAY, RecordingFetch(), now=NOW)["AAPL"]

    assert len(reads) == 1, "the second and third look-ups come from the memo"
    assert second is first
    assert len(third) == len(FULL)


def test_the_memo_notices_a_file_written_by_someone_else(cache: BarCache) -> None:
    warm(cache, MID_DAY, source=FULL.loc[: str(MID_DAY)])
    assert len(cache.read("AAPL")) == 30

    other = BarCache(cache.root)
    other.get_bars(["AAPL"], FIRST_DAY, LAST_DAY, RecordingFetch(), now=NOW)

    assert len(cache.read("AAPL")) == len(FULL), "mtime and size key the memo, not the symbol"


def test_the_memo_is_dropped_before_it_can_grow_without_bound(tmp_path: Path) -> None:
    cache = BarCache(tmp_path / "daily", memo_limit=2)
    for symbol in ("AAPL", "MSFT", "NVDA"):
        cache.get_bars([symbol], FIRST_DAY, LAST_DAY, RecordingFetch(), now=NOW)
    assert len(cache._memo) <= 2


def test_abandoned_temp_files_are_swept_on_the_next_fetch(cache: BarCache) -> None:
    """Audit LEAK-002: each orphan is a full symbol history left by a SIGKILL."""
    cache.root.mkdir(parents=True, exist_ok=True)
    stale = cache.root / "AAPL.parquet1234.tmp"
    fresh = cache.root / "MSFT.parquet5678.tmp"
    stale.write_bytes(b"half a parquet")
    fresh.write_bytes(b"half a parquet")
    two_days_ago = time.time() - 2 * 86_400
    os.utime(stale, (two_days_ago, two_days_ago))

    cache.get_bars(["AAPL"], FIRST_DAY, LAST_DAY, RecordingFetch(), now=NOW)

    assert not stale.exists()
    assert fresh.exists(), "a temp file from a write happening right now is not litter"


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
    with pytest.raises(ValueError, match="positive amount of time"):
        TtlJsonCache(tmp_path / "x.json", timedelta(days=1), miss_ttl=timedelta(0))


def test_a_negative_answer_expires_sooner_than_a_real_one(tmp_path: Path) -> None:
    """Audit BUG-051: "no earnings date" cached 3 days against a 10-day blackout.

    A date published inside that window admitted exactly the entry the
    blackout exists to block, so a miss now goes stale in hours.
    """
    cache = TtlJsonCache(tmp_path / "e.json", timedelta(days=3), miss_ttl=timedelta(hours=12))
    answers: dict[str, str | None] = {"KNOWN": "2026-09-01", "UNKNOWN": None}
    calls: list[list[str]] = []

    def fetch(keys: list[str]) -> dict[str, str | None]:
        calls.append(sorted(keys))
        return {key: answers[key] for key in keys}

    cache.get_or_fetch(["KNOWN", "UNKNOWN"], fetch, now=NOW)
    later = NOW + timedelta(hours=13)
    answers["UNKNOWN"] = "2026-08-20"
    out = cache.get_or_fetch(["KNOWN", "UNKNOWN"], fetch, now=later)

    assert calls[1] == ["UNKNOWN"], "the real answer is still fresh, the miss is not"
    assert out == {"KNOWN": "2026-09-01", "UNKNOWN": "2026-08-20"}


def test_long_dead_entries_are_dropped_when_the_file_is_written(tmp_path: Path) -> None:
    """Audit LEAK-003: symbols leave the universe; their entries never did."""
    cache = TtlJsonCache(tmp_path / "e.json", timedelta(days=3))
    cache.get_or_fetch(["GONE"], lambda keys: {"GONE": "x"}, now=NOW - timedelta(days=40))
    cache.get_or_fetch(["HERE"], lambda keys: {"HERE": "y"}, now=NOW)

    assert sorted(cache.read_all()) == ["HERE"], "40 days is far past 10 times a 3-day TTL"


def test_a_value_saved_by_another_process_mid_fetch_is_not_clobbered(tmp_path: Path) -> None:
    """Audit LEAK-003: the read-modify-write now re-reads under the lock.

    The scan holds this cache open across minutes of downloading while the
    morning confirm writes to the same file; the old code merged into the
    snapshot it had read *before* the fetch and saved that.
    """
    cache = TtlJsonCache(tmp_path / "e.json", timedelta(days=3))
    cache.get_or_fetch(["OLD"], lambda keys: {"OLD": "old"}, now=NOW)

    def fetch(keys: list[str]) -> dict[str, str]:
        other = TtlJsonCache(cache.path, cache.ttl)
        other.get_or_fetch(["SIDE"], lambda k: {"SIDE": "side"}, now=NOW)
        return {"NEW": "new"}

    cache.get_or_fetch(["NEW"], fetch, now=NOW)

    assert sorted(cache.read_all()) == ["NEW", "OLD", "SIDE"]


def test_a_long_cold_walk_is_persisted_chunk_by_chunk(tmp_path: Path) -> None:
    """Audit PERF-003: a Ctrl-C at symbol 1,400 used to discard all 1,400."""
    cache = TtlJsonCache(tmp_path / "e.json", timedelta(days=3))
    keys = [f"S{i}" for i in range(5)]
    calls: list[list[str]] = []

    def fetch(batch: list[str]) -> dict[str, str]:
        calls.append(list(batch))
        if len(calls) == 3:
            raise KeyboardInterrupt("the user gave up")
        return dict.fromkeys(batch, "value")

    with pytest.raises(KeyboardInterrupt):
        cache.get_or_fetch(keys, fetch, now=NOW, chunk_size=2)

    assert calls == [["S0", "S1"], ["S2", "S3"], ["S4"]]
    assert sorted(cache.read_all()) == ["S0", "S1", "S2", "S3"], "four survive the interrupt"


# ---------------------------------------------------------------------------
# per-call lifetimes (audit REPRO-1)
# ---------------------------------------------------------------------------


def test_a_caller_may_lengthen_one_lookup(tmp_path: Path) -> None:
    """The cache cannot know which half of a record a caller is reading.

    Historical earnings live in the same file as next quarter's estimate. A
    caller asking only about 2019 knows that answer is immutable; the cache
    does not, and used to expire the lot after three days (audit REPRO-1).
    """
    cache = TtlJsonCache(tmp_path / "e.json", timedelta(days=3))
    calls: list[list[str]] = []

    def fetch(keys: list[str]) -> dict[str, str]:
        calls.append(sorted(keys))
        return dict.fromkeys(keys, "2019-05-02")

    cache.get_or_fetch(["AAPL"], fetch, now=NOW)
    much_later = NOW + timedelta(days=400)

    assert cache.get_or_fetch(["AAPL"], fetch, now=much_later, ttl=timedelta(days=500)) == {
        "AAPL": "2019-05-02"
    }
    assert len(calls) == 1

    cache.get_or_fetch(["AAPL"], fetch, now=much_later)
    assert len(calls) == 2, "without the override the cache's own three days apply"


def test_a_per_call_lifetime_never_extends_a_cached_miss(tmp_path: Path) -> None:
    """Audit BUG-051 survives REPRO-1.

    A miss is an absence of data, and an absence is never settled: it is the
    one answer that can turn into a real one at any moment. However long a
    caller would like to trust the rest of the file, a ``None`` keeps its
    ``miss_ttl``.
    """
    cache = TtlJsonCache(tmp_path / "e.json", timedelta(days=3), miss_ttl=timedelta(hours=12))
    answers: dict[str, str | None] = {"KNOWN": "2019-05-02", "UNKNOWN": None}
    calls: list[list[str]] = []

    def fetch(keys: list[str]) -> dict[str, str | None]:
        calls.append(sorted(keys))
        return {key: answers[key] for key in keys}

    forever = timedelta(days=500)
    cache.get_or_fetch(["KNOWN", "UNKNOWN"], fetch, now=NOW, ttl=forever)
    later = NOW + timedelta(hours=13)
    cache.get_or_fetch(["KNOWN", "UNKNOWN"], fetch, now=later, ttl=forever)

    assert calls[1] == ["UNKNOWN"], "the override cannot make 'we found nothing' stick"


def test_ttl_for_reports_both_kinds_of_lifetime(tmp_path: Path) -> None:
    cache = TtlJsonCache(tmp_path / "e.json", timedelta(days=3), miss_ttl=timedelta(hours=12))
    override = timedelta(days=500)

    assert cache.ttl_for("a value") == timedelta(days=3)
    assert cache.ttl_for("a value", ttl=override) == override
    assert cache.ttl_for(None) == timedelta(hours=12)
    assert cache.ttl_for(None, ttl=override) == timedelta(hours=12)


def test_a_per_call_lifetime_does_not_change_what_is_pruned(tmp_path: Path) -> None:
    """Overriding a lookup must not shorten — or lengthen — a life on disk.

    Pruning is a property of the file, so it stays on the cache's own TTL.
    Anything else would let one caller's opinion about one window quietly
    delete another caller's data.
    """
    cache = TtlJsonCache(tmp_path / "e.json", timedelta(days=3))
    cache.get_or_fetch(["OLD"], lambda keys: {"OLD": "x"}, now=NOW - timedelta(days=40))

    cache.get_or_fetch(["NEW"], lambda keys: {"NEW": "y"}, now=NOW, ttl=timedelta(days=500))

    assert sorted(cache.read_all()) == ["NEW"], "40 days is still past 10 times a 3-day TTL"


# ---------------------------------------------------------------------------
# earnings_fingerprint (audit REPRO-1)
# ---------------------------------------------------------------------------

AAPL_DAYS = (date(2019, 2, 1), date(2019, 5, 2), date(2019, 8, 1))
MSFT_DAYS = (date(2019, 1, 30), date(2019, 4, 25))


def test_earnings_fingerprint_is_a_full_width_digest() -> None:
    """Same width as ``data_hash`` and ``config_hash``, so reports can align."""
    digest = earnings_fingerprint(["AAPL"], {"AAPL": AAPL_DAYS})

    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")


def test_earnings_fingerprint_ignores_every_kind_of_order() -> None:
    """Order-independence in all three places it could leak in."""
    canonical = earnings_fingerprint(["AAPL", "MSFT"], {"AAPL": AAPL_DAYS, "MSFT": MSFT_DAYS})

    assert (
        earnings_fingerprint(
            ["msft", "aapl"],  # the symbol list, and its casing
            {"MSFT": MSFT_DAYS, "AAPL": tuple(reversed(AAPL_DAYS))},  # dict and date order
        )
        == canonical
    )
    assert earnings_fingerprint(
        ["AAPL", "MSFT", "AAPL"], {"AAPL": AAPL_DAYS, "MSFT": MSFT_DAYS}
    ) == (canonical), "a duplicate symbol is still one symbol"
    assert (
        earnings_fingerprint(
            ["AAPL", "MSFT"], {"AAPL": (*AAPL_DAYS, AAPL_DAYS[0]), "MSFT": MSFT_DAYS}
        )
        == canonical
    ), "a duplicate date is still one date"


def test_earnings_fingerprint_moves_when_one_date_moves() -> None:
    """One day, on one symbol, out of a universe of many."""
    universe = [f"S{i}" for i in range(200)]
    before = dict.fromkeys(universe, AAPL_DAYS)
    after = {**before, "S137": (date(2019, 2, 1), date(2019, 5, 3), date(2019, 8, 1))}

    assert earnings_fingerprint(universe, before) != earnings_fingerprint(universe, after)


def test_earnings_fingerprint_moves_when_a_symbol_gains_or_loses_dates() -> None:
    known = earnings_fingerprint(["AAPL"], {"AAPL": AAPL_DAYS})

    assert earnings_fingerprint(["AAPL"], {"AAPL": AAPL_DAYS[:2]}) != known
    assert earnings_fingerprint(["AAPL"], {"AAPL": ()}) != known


def test_earnings_fingerprint_covers_the_symbols_asked_for(tmp_path: Path) -> None:
    """The universe is an input, and the cache's other keys are not.

    Two runs over different universes are different runs even when both got
    nothing back; a symbol sitting in the shared cache file that this run never
    looked up cannot change its answer.
    """
    nothing: dict[str, tuple[date, ...]] = {}

    assert earnings_fingerprint(["AAPL"], nothing) != earnings_fingerprint(
        ["AAPL", "MSFT"], nothing
    )
    assert earnings_fingerprint(["AAPL"], {"AAPL": AAPL_DAYS, "TSLA": MSFT_DAYS}) == (
        earnings_fingerprint(["AAPL"], {"AAPL": AAPL_DAYS})
    )


def test_earnings_fingerprint_treats_the_three_spellings_of_unknown_alike() -> None:
    """``None``, ``()`` and "absent" all block nothing, so all digest alike.

    :func:`swing.strategy.rules.earnings_blackout` cannot tell them apart, so a
    digest that could would raise the alarm on a difference the simulation is
    incapable of observing.
    """
    absent = earnings_fingerprint(["AAPL", "MSFT"], {"AAPL": AAPL_DAYS})
    empty = earnings_fingerprint(["AAPL", "MSFT"], {"AAPL": AAPL_DAYS, "MSFT": ()})
    none = earnings_fingerprint(["AAPL", "MSFT"], {"AAPL": AAPL_DAYS, "MSFT": None})

    assert absent == empty == none


def test_earnings_fingerprint_reads_both_provider_shapes() -> None:
    """``earnings_history`` returns sequences, ``earnings_dates`` a lone date.

    The backtest degrades from the first to the second when a provider cannot
    supply history, so the digest accepts either without the caller branching.
    """
    one_day = date(2019, 5, 2)
    from_dates = earnings_fingerprint(["AAPL"], {"AAPL": one_day})
    from_history = earnings_fingerprint(["AAPL"], {"AAPL": (one_day,)})

    assert from_dates == from_history
    assert earnings_fingerprint(["AAPL"], {"AAPL": pd.Timestamp("2019-05-02")}) == from_dates
    assert earnings_fingerprint(["AAPL"], {"AAPL": "2019-05-02"}) == from_dates


def test_earnings_fingerprint_ignores_missing_entries_inside_a_sequence() -> None:
    assert earnings_fingerprint(["AAPL"], {"AAPL": [*AAPL_DAYS, pd.NaT, None]}) == (
        earnings_fingerprint(["AAPL"], {"AAPL": AAPL_DAYS})
    )


def test_earnings_fingerprint_refuses_a_value_it_cannot_read() -> None:
    """A digest is a claim about what was consumed; guessing would be worse."""
    with pytest.raises(TypeError, match="Expected a date"):
        earnings_fingerprint(["AAPL"], {"AAPL": 42})


# ---------------------------------------------------------------------------
# earnings_coverage (audit COVER-1)
# ---------------------------------------------------------------------------


def test_earnings_coverage_counts_symbols_and_announcements() -> None:
    out = earnings_coverage(
        ["AAPL", "MSFT", "SPY"], {"AAPL": AAPL_DAYS, "MSFT": MSFT_DAYS, "SPY": ()}
    )

    assert (out.requested, out.with_dates, out.without_dates) == (3, 2, 1)
    assert out.announcements == len(AAPL_DAYS) + len(MSFT_DAYS)
    assert out.fraction == pytest.approx(2 / 3)


def test_earnings_coverage_counts_the_three_spellings_of_unknown_as_uncovered() -> None:
    """The blackout blocks nothing for all three, so all three are uncovered."""
    out = earnings_coverage(["A", "B", "C", "D"], {"A": AAPL_DAYS, "B": None, "C": ()})

    assert out.with_dates == 1
    assert out.without_dates == 3, "None, empty and absent all count the same"


def test_earnings_coverage_describes_the_real_cache_honestly() -> None:
    """The sentence that stops ``earnings_blackout_simulated: true`` misleading.

    Audit COVER-1: the flag says the mechanism ran. On the cache as it actually
    stood, the mechanism could reach 8% of the universe, and nothing in the
    report said so.
    """
    universe = [f"S{i}" for i in range(1642)]
    poisoned = {f"S{i}": AAPL_DAYS for i in range(135)}

    out = earnings_coverage(universe, poisoned)

    assert out.fraction == pytest.approx(135 / 1642, abs=1e-4)
    assert "135 of 1642 symbols (8%)" in out.describe()
    assert "could not apply to the remaining 1507" in out.describe()


def test_earnings_coverage_is_order_independent_and_case_insensitive() -> None:
    first = earnings_coverage(["AAPL", "msft"], {"aapl": AAPL_DAYS, "MSFT": MSFT_DAYS})
    second = earnings_coverage(["MSFT", "aapl"], {"AAPL": AAPL_DAYS, "msft": MSFT_DAYS})

    assert first == second


def test_earnings_coverage_of_nothing_is_zero_not_a_crash() -> None:
    out = earnings_coverage([], {})

    assert (out.requested, out.with_dates, out.fraction) == (0, 0, 0.0)
    assert "No symbols" in out.describe()


def test_earnings_coverage_serialises_for_a_run_summary() -> None:
    out = earnings_coverage(["AAPL", "SPY"], {"AAPL": AAPL_DAYS})

    assert out.as_json() == {
        "requested": 2,
        "with_dates": 1,
        "without_dates": 1,
        "announcements": 3,
        "fraction": 0.5,
    }
    assert json.dumps(out.as_json()), "must survive a trip through summary.json"
