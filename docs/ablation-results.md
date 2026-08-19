# Ablation Results

Out-of-sample (walk-forward) results for each one-change-at-a-time ablation of the Trend-Momentum
Core strategy.

TBD — run `scripts/ablations.py`.

```
uv run python scripts/ablations.py --universe etf
uv run python scripts/ablations.py --universe stocks
uv run python scripts/ablations.py --universe etf --quick   # short window, smoke test
```

The script overwrites this file with the generated table. The set of variants and the rationale for
each is defined in [`indicator-research.md` § Ablation plan](indicator-research.md#ablation-plan);
how to read the numbers — and why the best-scoring variant must **not** be selected as the shipping
configuration — is in [`indicator-research.md` §13](indicator-research.md#13-cross-cutting-caveat-data-snooping-and-edge-decay)
and [`backtest-methodology.md` §5](backtest-methodology.md#5-walk-forward-design).
