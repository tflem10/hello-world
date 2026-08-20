"""``swing doctor`` — the preflight that decides whether a new machine can work.

These tests exist because doctor's whole value is being trustworthy on a
machine nobody has debugged yet. A preflight that reports "ok" when the
provider is unreachable, or that silently makes network calls when asked not
to, is worse than no preflight: it converts an obvious failure into a
forty-minute backfill that returns nothing.

The network is never touched here — ``probe_provider`` is the single seam and
it is always monkeypatched, including in the test that proves ``--offline``
does not call it at all.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from swing import doctor
from swing.config import Config, load_config
from swing.data.cache import BarCache

from .conftest import make_bars


@pytest.fixture
def doc_config(tmp_path) -> Config:
    data = load_config().as_dict()
    data["data"]["cache_dir"] = str(tmp_path / "cache")
    data["reports"]["dir"] = str(tmp_path / "reports")
    return Config(data)


def _statuses(report) -> dict[str, str]:
    return {c.name: c.status for c in report.checks}


def _check(report, name: str):
    return next(c for c in report.checks if c.name == name)


def _seed_cache(cfg: Config, provider: str = "yfinance", age_days: int = 0, n: int = 30):
    """A cache whose newest bar is `age_days` old, stamped with `provider`."""
    cache = BarCache(cfg.expand_path(cfg.data.cache_dir))
    end = date.today() - timedelta(days=age_days)
    # Start far enough back that `n` business days overshoot `end`, then trim,
    # so the newest bar is the last business day on or before `end` regardless
    # of which weekday the test runs on.
    bars = make_bars([100.0] * n, start=str(end - timedelta(days=n)))
    bars = bars[bars.index <= str(end)]
    assert len(bars), "seed produced no bars on or before the target date"
    cache.write("AAA", bars)
    cache.stamp_provider(provider)
    return cache


# ---------------------------------------------------------------------------
# the offline contract
# ---------------------------------------------------------------------------
def test_offline_makes_no_network_call_at_all(doc_config, monkeypatch):
    """--offline is a promise, not a preference.

    It is what you run on a plane, behind a captive portal, or in CI. If it
    probed anyway the command would hang exactly where it is least welcome.
    """
    def _explode(*args, **kwargs):
        raise AssertionError("probe_provider was called despite --offline")

    monkeypatch.setattr(doctor, "probe_provider", _explode)
    monkeypatch.setattr(doctor, "probe_url", _explode)

    report = doctor.build_report(doc_config, offline=True)
    assert _statuses(report)["network"] == doctor.SKIP


def test_online_probes_the_configured_provider_and_the_fallback(doc_config, monkeypatch):
    """Both, deliberately: finding out the fallback is also down at 17:30 on a
    night yfinance breaks is finding out too late."""
    seen = []

    def _probe(cfg, name):
        seen.append(name)
        return True, "7 bars, latest 2024-05-01"

    monkeypatch.setattr(doctor, "probe_provider", _probe)
    monkeypatch.setattr(doctor, "probe_url", lambda url, timeout=15.0: (True, "HTTP 200"))

    report = doctor.build_report(doc_config, offline=False)
    assert seen == ["yfinance", "stooq"]
    statuses = _statuses(report)
    assert statuses["provider:yfinance"] == doctor.OK
    assert statuses["provider:stooq"] == doctor.OK
    assert statuses["universe source"] == doctor.OK


def test_a_down_fallback_warns_but_does_not_block(doc_config, monkeypatch):
    """The fallback being unavailable is information, not a blocker — the
    configured provider is working, so the system can run today."""
    monkeypatch.setattr(
        doctor, "probe_provider",
        lambda cfg, name: (True, "7 bars") if name == "yfinance" else (False, "HTTP 429"),
    )
    monkeypatch.setattr(doctor, "probe_url", lambda url, timeout=15.0: (True, "HTTP 200"))

    report = doctor.build_report(doc_config, offline=False)
    assert _statuses(report)["provider:stooq"] == doctor.WARN
    assert report.failed == []


def test_an_unreachable_provider_fails_and_names_the_fallback(doc_config, monkeypatch):
    """The failure a new machine actually hits, and the one worth being loud about."""
    monkeypatch.setattr(
        doctor, "probe_provider",
        lambda cfg, name: (False, "ConnectionError: getaddrinfo failed"),
    )
    monkeypatch.setattr(doctor, "probe_url", lambda url, timeout=15.0: (False, "HTTP 403"))

    report = doctor.build_report(doc_config, offline=False)
    check = _check(report, "provider:yfinance")

    assert check.status == doctor.FAIL
    assert "ConnectionError" in check.detail
    # The fix has to name the two things that produce this exact symptom, and
    # the escape hatch.
    assert "captive portal" in check.fix or "proxy" in check.fix
    assert "stooq" in check.fix
    # ...and it must not suggest switching providers without the cache warning.
    assert "data/cache" in check.fix


def test_a_reachable_provider_that_returns_nothing_is_not_ok(doc_config, monkeypatch):
    """'Reachable' is not the question. 'Did it give me bars' is."""
    monkeypatch.setattr(
        doctor, "probe_provider",
        lambda cfg, name: (False, "reachable but returned no bars for SPY"),
    )
    monkeypatch.setattr(doctor, "probe_url", lambda url, timeout=15.0: (True, "HTTP 200"))
    report = doctor.build_report(doc_config, offline=False)
    assert _statuses(report)["provider:yfinance"] == doctor.FAIL


# ---------------------------------------------------------------------------
# the probe itself
# ---------------------------------------------------------------------------
def test_the_probe_reports_a_bot_wall_as_a_block_not_as_missing_bars(
    doc_config, monkeypatch
):
    """A provider that knows it was refused says so, or you debug the wrong thing."""

    class Blocked:
        name = "stooq"
        blocked = True

        def __init__(self, cfg):
            pass

        def daily_bars(self, symbols, start, end):
            return {}

    from swing.data import provider as provider_module

    monkeypatch.setattr(provider_module, "get_provider", Blocked)
    reachable, detail = doctor.probe_provider(doc_config, "stooq")

    assert reachable is False
    assert "blocked" in detail


def test_the_probe_reads_a_refusal_status_as_a_wall_not_a_missing_symbol(
    doc_config, monkeypatch
):
    """The shape the live wall actually takes for this client: a bare 404.

    SPY certainly exists, so a 404 for it is about who is asking, not about the
    ticker — and the detail has to say which, or the operator goes hunting for a
    delisting that never happened.
    """

    class Refused:
        name = "stooq"
        http_status = {"SPY": 404}

        def __init__(self, cfg):
            pass

        def daily_bars(self, symbols, start, end):
            return {}

    from swing.data import provider as provider_module

    monkeypatch.setattr(provider_module, "get_provider", Refused)
    reachable, detail = doctor.probe_provider(doc_config, "stooq")

    assert reachable is False
    assert "404" in detail and "bot wall" in detail


def test_the_probe_and_the_real_provider_agree_about_a_refusal(doc_config, monkeypatch):
    """The two halves wired together: the provider records, the probe concludes.

    Every other test in this section fakes the provider, so all of them would
    keep passing if the attribute were renamed on one side only. This one runs
    the real StooqProvider with nothing but `requests.get` stubbed out.
    """
    import requests

    class FakeResponse:
        status_code = 404
        text = ""

    monkeypatch.setattr(
        requests, "get", lambda url, params=None, timeout=None: FakeResponse()
    )

    reachable, detail = doctor.probe_provider(doc_config, "stooq")
    assert reachable is False
    assert "404" in detail and "bot wall" in detail


def test_the_probe_does_not_call_a_server_error_a_bot_wall(doc_config, monkeypatch):
    """A 500 is the provider being broken, not the provider refusing us."""

    class Broken:
        name = "stooq"
        http_status = {"SPY": 500}

        def __init__(self, cfg):
            pass

        def daily_bars(self, symbols, start, end):
            return {}

    from swing.data import provider as provider_module

    monkeypatch.setattr(provider_module, "get_provider", Broken)
    reachable, detail = doctor.probe_provider(doc_config, "stooq")

    assert reachable is False
    assert "500" in detail and "bot wall" not in detail


def test_the_probe_still_says_no_bars_when_that_is_all_it_knows(doc_config, monkeypatch):
    class Empty:
        name = "yfinance"

        def __init__(self, cfg):
            pass

        def daily_bars(self, symbols, start, end):
            return {}

    from swing.data import provider as provider_module

    monkeypatch.setattr(provider_module, "get_provider", Empty)
    reachable, detail = doctor.probe_provider(doc_config, "yfinance")

    assert reachable is False
    assert "no bars" in detail


# ---------------------------------------------------------------------------
# cache checks
# ---------------------------------------------------------------------------
def test_empty_cache_warns_with_the_backfill_command(doc_config):
    report = doctor.build_report(doc_config, offline=True)
    check = _check(report, "price cache")
    assert check.status == doctor.WARN
    assert "backfill" in check.fix


def test_a_provider_mismatched_cache_is_a_hard_failure(doc_config):
    """Same rule the data layer enforces: two adjustment bases never mix.

    doctor must agree with `swing data`, or you get a green preflight followed
    by a refused backfill.
    """
    _seed_cache(doc_config, provider="stooq")
    report = doctor.build_report(doc_config, offline=True)
    check = _check(report, "price cache")

    assert check.status == doctor.FAIL
    assert "stooq" in check.detail and "yfinance" in check.detail
    assert "rm -rf" in check.fix


def test_a_current_cache_passes(doc_config):
    _seed_cache(doc_config, provider="yfinance", age_days=0)
    assert _statuses(doctor.build_report(doc_config, offline=True))["price cache"] == doctor.OK


def test_a_stale_cache_warns_with_the_update_command(doc_config):
    _seed_cache(doc_config, provider="yfinance", age_days=60)
    check = _check(doctor.build_report(doc_config, offline=True), "price cache")
    assert check.status == doctor.WARN
    assert "update" in check.fix


# ---------------------------------------------------------------------------
# gate and config
# ---------------------------------------------------------------------------
def test_a_missing_gate_report_warns_rather_than_fails(doc_config):
    """No walk-forward yet is the expected state of a fresh install, not a fault.

    `swing scan` enforces this for real; doctor only has to say it out loud.
    """
    check = _check(doctor.build_report(doc_config, offline=True), "backtest gate")
    assert check.status == doctor.WARN
    assert "walk-forward" in check.fix


def test_a_clean_pass_is_reported_as_ok(doc_config, monkeypatch):
    from swing.backtest import gate as gate_module

    monkeypatch.setattr(gate_module, "check_gate",
                        lambda cfg: gate_module.GateStatus(passed=True))
    check = _check(doctor.build_report(doc_config, offline=True), "backtest gate")
    assert check.status == doctor.OK
    assert "PASS" in check.detail


def test_a_pass_that_carries_a_caveat_warns_instead_of_going_quiet(
    doc_config, monkeypatch
):
    """An aged-out validation still passes on its numbers. Silence would bury it."""
    from swing.backtest import gate as gate_module

    monkeypatch.setattr(
        gate_module, "check_gate",
        lambda cfg: gate_module.GateStatus(
            passed=True, reasons=["this validation is 400 days old (generated 2025-07-14)"]
        ),
    )
    check = _check(doctor.build_report(doc_config, offline=True), "backtest gate")

    assert check.status == doctor.WARN
    assert "PASS" in check.detail and "400 days old" in check.detail
    assert "walk-forward" in check.fix


def test_a_gate_switched_off_in_config_is_not_reported_as_a_clean_pass(doc_config):
    """`enabled = false` passes everything; doctor has to say why it passed."""
    data = doc_config.as_dict()
    data["backtest"]["gate"]["enabled"] = False
    check = _check(doctor.build_report(Config(data), offline=True), "backtest gate")

    assert check.status == doctor.WARN
    assert "disabled" in check.detail
    assert "enabled = true" in check.fix


def test_the_config_hash_is_reported(doc_config):
    """The hash is what binds a gate pass to a parameter set; seeing it here
    saves a round trip when a report and a config disagree."""
    check = _check(doctor.build_report(doc_config, offline=True), "config hash")
    assert check.status == doctor.OK
    assert doc_config.hash in check.detail


# ---------------------------------------------------------------------------
# exit codes and rendering
# ---------------------------------------------------------------------------
def test_warnings_alone_exit_zero(doc_config, capsys):
    """A fresh install is all warnings. Exiting non-zero would make doctor
    useless in any script that checks its status."""
    assert doctor.run_doctor(doc_config, offline=True) == 0
    assert "no blockers" in capsys.readouterr().out


def test_any_failure_exits_one(doc_config, capsys):
    _seed_cache(doc_config, provider="stooq")      # guaranteed FAIL
    assert doctor.run_doctor(doc_config, offline=True) == 1
    assert "FAILED" in capsys.readouterr().out


def test_the_rendered_report_shows_fixes_for_failures(doc_config):
    _seed_cache(doc_config, provider="stooq")
    text = doctor.build_report(doc_config, offline=True).render()
    assert "swing doctor" in text
    assert "rm -rf" in text            # the fix, not just the complaint


def test_every_check_renders_one_line_with_a_known_marker(doc_config):
    report = doctor.build_report(doc_config, offline=True)
    assert report.checks
    for check in report.checks:
        line = check.line()
        assert "\n" not in line
        assert any(mark in line for mark in ("[ok  ]", "[warn]", "[FAIL]", "[skip]"))


def test_doctor_is_reachable_through_the_cli(doc_config, tmp_path, monkeypatch):
    """Proves the argparse wiring, not just the module."""
    from swing.cli import main

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        "[data]\n"
        f'cache_dir = "{tmp_path / "cache"}"\n'
        "[reports]\n"
        f'dir = "{tmp_path / "reports"}"\n'
    )
    assert main(["-c", str(cfg_path), "doctor", "--offline"]) == 0
