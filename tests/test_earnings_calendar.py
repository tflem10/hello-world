"""Loading a user-supplied historical earnings calendar.

The backtest applies the earnings blackout only when a calendar is handed to
it, so this loader is the whole bridge between "free data has no calendar" and
"backtest matches live". Every test writes its own CSV under ``tmp_path``;
nothing here touches the network or the shipped data files.
"""

from __future__ import annotations

import datetime as dt
import logging

import pytest

from swing.data.earnings_calendar import load_earnings_calendar


def write(tmp_path, text: str, name: str = "earnings.csv"):
    path = tmp_path / name
    path.write_text(text)
    return path


def test_happy_path_sorts_dedupes_and_uppercases(tmp_path):
    """The contract the engine relies on: symbol -> sorted, unique dates.

    Dates arrive out of order and duplicated (a common shape when two exports
    are concatenated), and a lower-case ticker must key the same slot as an
    upper-case one or the blackout silently misses that symbol.
    """
    path = write(
        tmp_path,
        "symbol,date\n"
        "aapl,2024-05-02\n"
        "AAPL,2024-02-01\n"
        "AAPL,2024-02-01\n"
        " msft ,2024-01-30\n",
    )
    assert load_earnings_calendar(path) == {
        "AAPL": [dt.date(2024, 2, 1), dt.date(2024, 5, 2)],
        "MSFT": [dt.date(2024, 1, 30)],
    }


def test_accepts_a_string_path(tmp_path):
    """The frozen signature takes ``str | Path``; the runner may pass either."""
    path = write(tmp_path, "symbol,date\nAAPL,2024-02-01\n")
    assert load_earnings_calendar(str(path)) == {"AAPL": [dt.date(2024, 2, 1)]}


def test_header_order_case_and_extra_columns(tmp_path):
    """Real exports put the columns in their own order with extra metadata.

    Matching the header case-sensitively or by position would make a perfectly
    good vendor file look malformed, so this must load unchanged.
    """
    path = write(
        tmp_path,
        "Fiscal_Quarter,Date,Time,SYMBOL\n"
        "Q1,2024-02-01,amc,AAPL\n"
        "Q2,2024-05-02,bmo,AAPL\n",
    )
    assert load_earnings_calendar(path) == {
        "AAPL": [dt.date(2024, 2, 1), dt.date(2024, 5, 2)]
    }


def test_comment_and_blank_lines_are_skipped(tmp_path):
    """Same convention as ``universe.read_symbol_file`` — users annotate files."""
    path = write(
        tmp_path,
        "# exported 2026-01-04 from vendor X\n"
        "\n"
        "symbol,date\n"
        "AAPL,2024-02-01\n"
        "\n"
        "# MSFT pending confirmation\n"
        "MSFT,2024-01-30\n",
    )
    assert load_earnings_calendar(path) == {
        "AAPL": [dt.date(2024, 2, 1)],
        "MSFT": [dt.date(2024, 1, 30)],
    }


def test_one_bad_row_is_skipped_and_warned_rest_still_loads(tmp_path, caplog):
    """A single dirty row must not cost the other 4,999 rows of an export.

    The warning has to name the line number of the *original* file, since that
    is the only thing the user can act on.
    """
    path = write(
        tmp_path,
        "symbol,date\n"          # line 1
        "AAPL,2024-02-01\n"      # line 2
        "MSFT,not-a-date\n"      # line 3  <- malformed
        "NVDA,2024-02-21\n"      # line 4
        "AMD,2024-01-30\n",      # line 5
    )
    with caplog.at_level(logging.WARNING, logger="swing.data.earnings_calendar"):
        calendar = load_earnings_calendar(path)

    assert calendar == {
        "AAPL": [dt.date(2024, 2, 1)],
        "NVDA": [dt.date(2024, 2, 21)],
        "AMD": [dt.date(2024, 1, 30)],
    }
    assert "MSFT" not in calendar
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "line 3" in warnings[0]
    assert "not-a-date" in warnings[0]


def test_empty_symbol_row_is_skipped_and_warned(tmp_path, caplog):
    """A blank ticker cannot be blacked out; it is a malformed row, not a key."""
    path = write(
        tmp_path,
        "symbol,date\nAAPL,2024-02-01\n,2024-03-01\nMSFT,2024-01-30\n",
    )
    with caplog.at_level(logging.WARNING, logger="swing.data.earnings_calendar"):
        calendar = load_earnings_calendar(path)

    assert set(calendar) == {"AAPL", "MSFT"}
    assert "" not in calendar
    assert any("line 3" in r.getMessage() for r in caplog.records)


def test_symbol_with_only_bad_dates_is_omitted_entirely(tmp_path):
    """No empty lists in the output — "absent" and "no events" mean the same
    thing to the engine, and an empty list would only invite ``KeyError``-free
    but meaningless iteration."""
    path = write(
        tmp_path,
        "symbol,date\nAAPL,2024-02-01\nAAPL,2024-05-02\nMSFT,????\nNVDA,2024-02-21\n",
    )
    calendar = load_earnings_calendar(path)
    assert "MSFT" not in calendar
    assert all(dates for dates in calendar.values())


def test_mostly_malformed_file_raises_value_error_naming_the_path(tmp_path):
    """The garbage guard: a wrong file must fail loudly, not load 2 of 6 rows.

    Silently returning a nearly-empty calendar is the worst outcome available —
    the engine would stop warning about the missing blackout while most of the
    universe stayed unprotected.
    """
    path = write(
        tmp_path,
        "symbol,date\n"
        "AAPL,2024-02-01\n"
        "MSFT,2024-01-30\n"
        "NVDA,garbage\n"
        "AMD,garbage\n"
        "INTC,garbage\n"
        "TSLA,garbage\n",
    )
    with pytest.raises(ValueError) as excinfo:
        load_earnings_calendar(path)
    message = str(excinfo.value)
    assert str(path) in message
    assert "4" in message  # the malformed count


def test_exactly_half_malformed_still_loads(tmp_path):
    """The guard is "more than half", so a 50/50 file is dirty, not garbage."""
    path = write(
        tmp_path,
        "symbol,date\nAAPL,2024-02-01\nMSFT,2024-01-30\nNVDA,x\nAMD,y\n",
    )
    assert set(load_earnings_calendar(path)) == {"AAPL", "MSFT"}


def test_missing_file_raises_file_not_found(tmp_path):
    """The caller decides whether a missing calendar is fatal or just means
    "run without the blackout and keep the divergence warning"."""
    with pytest.raises(FileNotFoundError):
        load_earnings_calendar(tmp_path / "nope.csv")


def test_header_only_file_is_an_empty_calendar_not_an_error(tmp_path):
    """A calendar you have not populated yet is valid input: zero blackouts,
    which is exactly today's behaviour, and no exception to special-case."""
    assert load_earnings_calendar(write(tmp_path, "symbol,date\n")) == {}
    assert load_earnings_calendar(write(tmp_path, "# nothing yet\nsymbol,date\n\n")) == {}


def test_header_without_required_columns_raises(tmp_path):
    """A file with neither column is the wrong file; loading it as an empty
    calendar would look like a successfully loaded (and useless) blackout."""
    path = write(tmp_path, "ticker,report_day\nAAPL,2024-02-01\n")
    with pytest.raises(ValueError) as excinfo:
        load_earnings_calendar(path)
    assert str(path) in str(excinfo.value)


def test_timestamped_dates_are_accepted(tmp_path):
    """Vendor exports often carry a time component; the day is what matters."""
    path = write(
        tmp_path,
        "symbol,date\nAAPL,2024-02-01T21:30:00\nMSFT,2024-01-30 16:05:00\n",
    )
    assert load_earnings_calendar(path) == {
        "AAPL": [dt.date(2024, 2, 1)],
        "MSFT": [dt.date(2024, 1, 30)],
    }


def test_quoted_and_short_rows(tmp_path):
    """Quoted fields parse; a truncated row is malformed, not an IndexError."""
    path = write(
        tmp_path,
        'symbol,date,note\n"AAPL","2024-02-01","beat, raised"\nMSFT\nNVDA,2024-02-21,\n',
    )
    assert load_earnings_calendar(path) == {
        "AAPL": [dt.date(2024, 2, 1)],
        "NVDA": [dt.date(2024, 2, 21)],
    }


def test_output_feeds_the_engine_blackout_mask(tmp_path):
    """End-to-end shape check: what the loader returns is what the engine's
    ``earnings=`` parameter expects (``dict[str, list[date]]``), so the wiring
    agent's integration cannot silently receive the wrong types."""
    path = write(tmp_path, "symbol,date\nAAPL,2024-02-01\n")
    calendar = load_earnings_calendar(path)
    assert isinstance(calendar, dict)
    for symbol, dates in calendar.items():
        assert isinstance(symbol, str)
        assert isinstance(dates, list)
        assert all(isinstance(d, dt.date) and not isinstance(d, dt.datetime) for d in dates)
