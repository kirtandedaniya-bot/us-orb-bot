"""Parity: replay rsi2_rules.decide() day by day and compare with the research backtest positions."""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import rsi2_rules as R  # noqa: E402
import strategy_tests as S  # noqa: E402

for t in ["SPY", "QQQ", "IWM", "DIA"]:
    d = S.load(t)
    c = d.Close
    # research positions (same loop as s_rsi2)
    sma200, sma5, r = c.rolling(200).mean(), c.rolling(5).mean(), S.rsi2(c)
    ref, held = [], 0
    for i in range(len(d)):
        if held == 0 and c.iloc[i] > sma200.iloc[i] and r.iloc[i] < 10:
            held = 1
        elif held == 1 and c.iloc[i] > sma5.iloc[i]:
            held = 0
        ref.append(held)
    # bot replay: only from MIN_BARS on (bot refuses shorter history); start flat there
    start = R.MIN_BARS
    held, mism, n = bool(ref[start - 1]), 0, 0
    for i in range(start, len(d)):
        held, _ = R.decide(c.iloc[: i + 1], held)
        n += 1
        mism += int(held) != ref[i]
    print(f"{t}: {n} days replayed, mismatches {mism}")
