"""Where the scan reports live, and which one is *the* current one.

Three commands ask the same question — ``swing confirm`` before re-quoting,
``swing execute`` before planning orders, and every test that reaches for last
night's sheet — and they used to ask it of two different implementations that
disagreed (audit DEBT-001): one sorted directory *names* lexicographically and
required a ``picks.json``, the other took the parsed-date maximum and looked at
nothing inside. On the states that actually happen — a crash halfway through a
write, a weekend run that produced nothing — they picked different directories,
so a confirm and an execute minutes apart could target different scans.

This module is the single answer (remediation contract A4). The rule is frozen
and deliberately dull, and its two halves are independent:

1. Only ``scan-YYYY-MM-DD`` directories count, matched by a strict regex, and
   they are ordered by the date *parsed* out of the name — never by the name
   itself, because a lexicographic sort is only accidentally chronological.
2. **Unconditionally**, ``picks.json`` must exist and parse. It is the last file
   the scan writes (audit BUG-020), so a directory without one is a crash
   artefact rather than a report, and no caller may opt out of that — a
   half-written directory being "latest" for one command and not for another is
   precisely the divergence DEBT-001 is about.
3. ``require_picks`` decides one thing only: whether a *parseable but empty*
   report (no picks and no watch entries) may win. With it True — the confirm's
   view — an empty report loses to an older one that holds something, which is
   what stops a Saturday run, which re-derives Friday's names and dedupes them
   all away, from shadowing Friday's actual picks on Monday morning (audit
   BUG-012); it is still returned when nothing else qualifies, so a genuinely
   empty night shows the user the empty report rather than "there is no scan to
   confirm". With it False — the executor's view — the newest parseable report
   wins whatever it contains, so today's empty scan produces a loud refusal
   instead of quietly re-executing a superseded older day.

:func:`prune_scan_dirs` implements the other half of the retention policy in
contract A17 (audit LEAK-002): one directory per calendar day accumulates
forever otherwise, and every lookup above walks all of them.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from datetime import date, timedelta
from pathlib import Path

__all__ = [
    "PICKS_FILENAME",
    "RETAIN_SCAN_DAYS",
    "SCAN_DIR_PREFIX",
    "latest_scan_dir",
    "prune_scan_dirs",
    "scan_dir_date",
    "scan_dirs",
]

log = logging.getLogger(__name__)

#: Every scan report directory is ``scan-YYYY-MM-DD``.
SCAN_DIR_PREFIX = "scan-"
_SCAN_DIR_RE = re.compile(r"^scan-(\d{4}-\d{2}-\d{2})$")

#: The file whose presence makes a directory a report — written last.
PICKS_FILENAME = "picks.json"

#: Scan directories older than this are deleted at the start of the next scan
#: (contract A17). Ninety days is well past any workflow that reads them.
RETAIN_SCAN_DAYS = 90


def scan_dir_date(path: Path) -> date | None:
    """The date a scan directory's name encodes, or ``None`` when it is not one.

    Strict on purpose: ``scan-2026-8-1``, ``scan-latest`` and ``backtest`` all
    return ``None`` rather than being coerced into an ordering.
    """
    match = _SCAN_DIR_RE.match(Path(path).name)
    if match is None:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:  # pragma: no cover - the regex already fixed the shape
        return None


def scan_dirs(reports_dir: Path) -> list[tuple[date, Path]]:
    """Every scan directory under ``reports_dir`` as ``(date, path)``, oldest first."""
    root = Path(reports_dir).expanduser()
    if not root.is_dir():
        return []
    dated: list[tuple[date, Path]] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        when = scan_dir_date(child)
        if when is not None:
            dated.append((when, child))
    dated.sort(key=lambda pair: pair[0])
    return dated


def _report_state(scan_dir: Path) -> tuple[bool, bool]:
    """``(picks.json parses, it holds at least one pick or watch entry)``."""
    picks_file = scan_dir / PICKS_FILENAME
    try:
        payload = json.loads(picks_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.debug("Ignoring %s: its %s could not be read (%s)", scan_dir, PICKS_FILENAME, exc)
        return False, False
    if not isinstance(payload, dict):
        return True, False
    picks = payload.get("picks")
    watch = payload.get("watch")
    populated = bool(isinstance(picks, list) and picks) or bool(isinstance(watch, list) and watch)
    return True, populated


def latest_scan_dir(reports_dir: Path, *, require_picks: bool = True) -> Path | None:
    """The scan directory a command should act on, or ``None`` when there is none.

    A candidate must **always** hold a ``picks.json`` that exists and parses;
    ``require_picks`` cannot switch that off, because a directory that crashed
    before its commit point is not a report for anybody (audit DEBT-001,
    BUG-020).

    Args:
        reports_dir: the configured ``paths.reports_dir``. A missing directory
            is not an error; it simply means no scan has ever run.
        require_picks: whether a parseable-but-empty report — no picks *and* no
            watch entries — may be the answer. True (the default, and what the
            morning confirm wants) skips it in favour of an older report that
            holds something, and returns it only when nothing else qualifies.
            False (what the executor wants) takes the newest parseable report
            whatever it contains, so an empty scan today becomes a refusal
            rather than a silent re-run of a superseded day.

    Returns:
        The chosen directory, or ``None``.
    """
    parsed: list[Path] = []
    populated: list[Path] = []
    for _when, path in scan_dirs(reports_dir):  # oldest first: the last append is newest
        readable, has_entries = _report_state(path)
        if not readable:
            continue
        parsed.append(path)
        if has_entries:
            populated.append(path)

    if not parsed:
        return None
    if not require_picks:
        return parsed[-1]
    return populated[-1] if populated else parsed[-1]


def prune_scan_dirs(
    reports_dir: Path, *, asof: date, keep_days: int = RETAIN_SCAN_DAYS
) -> list[Path]:
    """Delete scan directories older than ``keep_days`` before ``asof`` (audit LEAK-002).

    Only ``scan-YYYY-MM-DD`` directories are ever considered: a ``backtest``
    directory, a hand-made note or anything else the user keeps beside the
    reports is left exactly where it is.

    Args:
        reports_dir: the configured reports directory.
        asof: the date retention is measured back from — the scan's own
            ``asof``, never the wall clock, so pruning is deterministic.
        keep_days: how many days of reports to keep. ``0`` or less disables
            pruning entirely.

    Returns:
        The directories that were removed, oldest first. A directory that could
        not be deleted is logged and left alone rather than failing the scan.
    """
    if keep_days <= 0:
        return []
    cutoff = asof - timedelta(days=keep_days)
    removed: list[Path] = []
    for when, path in scan_dirs(reports_dir):
        if when >= cutoff:
            continue
        try:
            shutil.rmtree(path)
        except OSError as exc:  # pragma: no cover - permissions only
            log.warning("The old scan report %s could not be removed (%s)", path, exc)
            continue
        removed.append(path)
    if removed:
        log.info(
            "Removed %d scan report(s) older than %d days from %s",
            len(removed),
            keep_days,
            Path(reports_dir).expanduser(),
        )
    return removed
