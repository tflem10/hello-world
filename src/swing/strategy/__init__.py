"""Strategy rules, ranking and position sizing.

These modules are imported *unchanged* by both the backtester and the live
scanner. That is deliberate and it is the single most important structural
decision in this repo: it makes backtest/live divergence a compile error rather
than a slow, expensive surprise.
"""
