"""Console + file logging shared by the CLI, scheduler jobs and tests."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_CONFIGURED = False


class _Colors:
    RESET = "\033[0m"
    DIM = "\033[2m"
    RED = "\033[31m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"


class _ConsoleFormatter(logging.Formatter):
    LEVEL_COLOR = {
        logging.DEBUG: _Colors.DIM,
        logging.INFO: "",
        logging.WARNING: _Colors.YELLOW,
        logging.ERROR: _Colors.RED,
        logging.CRITICAL: _Colors.RED,
    }

    def __init__(self, color: bool):
        super().__init__("%(message)s")
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        if record.levelno >= logging.WARNING:
            msg = f"{record.levelname.lower()}: {msg}"
        if not self.color:
            return msg
        return f"{self.LEVEL_COLOR.get(record.levelno, '')}{msg}{_Colors.RESET}"


def setup_logging(verbose: bool = False, log_file: Path | None = None) -> None:
    """Idempotently configure the root logger."""
    global _CONFIGURED
    root = logging.getLogger()
    if _CONFIGURED:
        root.setLevel(logging.DEBUG if verbose else logging.INFO)
        return

    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(_ConsoleFormatter(color=sys.stderr.isatty()))
    root.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file)
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )
        root.addHandler(fh)

    # yfinance/urllib3 are chatty at INFO.
    for noisy in ("urllib3", "yfinance", "peewee", "matplotlib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
