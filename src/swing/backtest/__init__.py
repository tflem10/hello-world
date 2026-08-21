"""Daily-bar backtester built around the same rules the live scanner uses."""

from .engine import Backtester, BacktestResult, Trade, run_backtest

__all__ = ["BacktestResult", "Backtester", "Trade", "run_backtest"]
