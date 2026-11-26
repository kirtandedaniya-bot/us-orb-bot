"""explain_setup() must agree with build_setup(): None <=> a non-empty reason."""
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from orb.strategy import Params, build_setup, explain_setup, setup_context

P = Params()


def make_daily(n=40, drift=0.5, rng_w=4.0, start=200.0):
    c = start + drift * np.arange(n)
    return pd.DataFrame({"high": c + rng_w / 2, "low": c - rng_w / 2, "close": c})


def make_orb(n=45, start=210.0, step=0.02, width=0.3, vol=1000.0, last=None):
    idx = [datetime(2026, 11, 25, 9, 30) + timedelta(minutes=i) for i in range(n)]
    c = start + step * np.arange(n)
    if last is not None and n:
        c[-1] = last
    return pd.DataFrame({"high": c + width, "low": c - width, "close": c,
                         "volume": np.full(n, vol)}, index=pd.DatetimeIndex(idx))


def check(orb, daily, p=P):
    s = build_setup("TEST", orb, daily, p)
    why = explain_setup("TEST", orb, daily, p)
    assert (s is None) == bool(why), (s, why)
    return s, why


def test_valid_setup_gives_empty_reason():
    s, why = check(make_orb(), make_daily())
    assert s is not None and why == ""
    assert "trend up" in setup_context(make_orb(), make_daily())


@pytest.mark.parametrize("orb,daily,p,expect", [
    (make_orb(n=38), make_daily(), P, "too few ORB bars (n=38 < 40)"),
    (make_orb(vol=0.0), make_daily(), P, "no ORB volume"),
    (make_orb(), make_daily(n=10), P, "trend flat/unknown"),
    (make_orb(width=0.05, step=0.0), make_daily(), P, "outside 25-60"),
    (make_orb(width=3.0), make_daily(), P, "outside 25-60"),
    (make_orb(last=210.4), make_daily(), P, "trend up but 10:14 close 210.40 <= VWAP 210.43"),
    (make_orb(step=-0.02, start=211.0, last=211.0), make_daily(drift=-0.5, start=240.0), P, "trend down but 10:14 close 211.00 >= VWAP"),
    (make_orb(), make_daily(), Params(min_risk_ps=50.0), "risk/share"),
    (make_orb(), make_daily(), Params(max_notional=10.0), "shares < 1"),
])
def test_each_rejection_has_reason(orb, daily, p, expect):
    s, why = check(orb, daily, p)
    assert s is None and expect in why, why


def test_random_agreement():
    rng = np.random.default_rng(7)
    found = rejected = 0
    for _ in range(3000):
        daily = make_daily(n=int(rng.integers(15, 40)), drift=float(rng.normal(0, 0.6)),
                           rng_w=float(rng.uniform(0.5, 8)), start=float(rng.uniform(20, 500)))
        start = float(daily["close"].iloc[-1]) * float(rng.uniform(0.97, 1.03))
        orb = make_orb(n=int(rng.integers(35, 46)), start=start, step=float(rng.normal(0, 0.05)),
                       width=float(rng.uniform(0.01, 2.0)), vol=float(rng.choice([0.0, 500.0, 1e5])),
                       last=start * float(rng.uniform(0.99, 1.01)))
        p = Params(min_risk_ps=float(rng.choice([0.02, 0.5, 3.0])),
                   max_notional=float(rng.choice([20_000.0, 50.0])), trend_len=int(rng.choice([20, 0])))
        s, _ = check(orb, daily, p)
        found += s is not None
        rejected += s is None
    assert found > 20 and rejected > 20   # both paths exercised
