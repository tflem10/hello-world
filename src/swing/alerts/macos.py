"""macOS notification banner via osascript. A no-op everywhere else."""

from __future__ import annotations

import shutil
import subprocess
import sys

from ..logging_setup import get_logger

log = get_logger("swing.alerts.macos")


def available() -> bool:
    return sys.platform == "darwin" and shutil.which("osascript") is not None


def send(title: str, message: str, subtitle: str = "") -> None:
    """Show a notification banner. Raises if osascript is unavailable or fails."""
    if not available():
        raise RuntimeError("macOS notifications require osascript on darwin")

    script = (
        f'display notification "{_escape(message)}" '
        f'with title "{_escape(title)}"'
    )
    if subtitle:
        script += f' subtitle "{_escape(subtitle)}"'

    result = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True, timeout=15, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "osascript failed")


def _escape(text: str) -> str:
    """AppleScript string literals: escape backslashes and quotes, collapse newlines."""
    cleaned = " ".join(str(text).split())
    return cleaned.replace("\\", "\\\\").replace('"', '\\"')[:400]
