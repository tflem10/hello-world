"""FROZEN CONTRACT 11 — backtesting, walk-forward analysis and the trading gate.

The modules are deliberately not re-exported here: ``runner`` pulls in the data
layer and ``report`` pulls in matplotlib, and ``swing.cli`` imports this package
on every command. Import what you need directly::

    from swing.backtest import gate
    from swing.backtest.runner import run_backtest

Module map:

* :mod:`~swing.backtest.engine` — the daily-bar portfolio simulator (the audit
  surface; its docstring is the specification).
* :mod:`~swing.backtest.costs` — per-side slippage and spread.
* :mod:`~swing.backtest.metrics` — CAGR, Sharpe, profit factor and friends.
* :mod:`~swing.backtest.walkforward` — folds, the frozen tuning grid, the
  selection objective and the sensitivity table.
* :mod:`~swing.backtest.runner` — ``swing backtest``: data in, report directory
  out.
* :mod:`~swing.backtest.report` — Markdown/HTML rendering and ``swing report``.
* :mod:`~swing.backtest.gate` — the mechanical permission to emit picks.
"""
