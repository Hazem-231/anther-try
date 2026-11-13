"""Offline fixtures: synthetic hourly bars, a fake data provider, a small config."""
from __future__ import annotations

import copy
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quantbot.config import load_config  # noqa: E402


def make_bars(asset_class: str, days: int = 260, seed: int = 1, drift: float = 0.0, start_price: float = 100.0,
              end: str = "2026-11-06") -> pd.DataFrame:
    """Random-walk hourly bars. Stocks: 7 bars per weekday from 14:30 UTC."""
    rng = np.random.default_rng(seed)
    if asset_class == "stocks":
        day_index = pd.bdate_range(end=end, periods=days, tz="UTC")
        idx = pd.DatetimeIndex([d + pd.Timedelta(hours=14, minutes=30) + pd.Timedelta(hours=k)
                                for d in day_index for k in range(7)])
    else:
        idx = pd.date_range(end=pd.Timestamp(end, tz="UTC") + pd.Timedelta(hours=23), periods=days * 24, freq="h")
    n = len(idx)
    ret = rng.normal(drift, 0.004, n)
    close = start_price * np.exp(np.cumsum(ret))
    open_ = np.concatenate([[start_price], close[:-1]]) * np.exp(rng.normal(0, 0.0005, n))
    spread = np.abs(rng.normal(0, 0.002, n))
    high = np.maximum(open_, close) * (1 + spread)
    low = np.minimum(open_, close) * (1 - spread)
    volume = rng.integers(1_000, 100_000, n).astype(float)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx)


class FakeData:
    name = "fake"
    stock_bar_minute = 30

    def __init__(self, frames: dict[str, pd.DataFrame]):
        self.frames = frames
        self.last_price = {s: float(df["close"].iloc[-1]) for s, df in frames.items()}

    def bars(self, symbol, asset_class, days, now=None):
        return self.frames[symbol]

    def latest_price(self, symbol, asset_class):
        return self.last_price[symbol]


@pytest.fixture
def cfg():
    c = copy.deepcopy(load_config())
    c["universe"] = {"stocks": ["AAA", "BBB", "CCC"], "crypto": ["XXX/USD", "YYY/USD"]}
    c["benchmarks"] = {"stocks": "AAA", "crypto": "XXX/USD"}
    c["timemachine"]["replays"] = 500
    c["backtest"].update({"days": 260, "warmup_days": 120, "refit_days": 40, "replays": 200})
    return c


@pytest.fixture
def frames(cfg):
    out = {}
    for k, s in enumerate(cfg["universe"]["stocks"]):
        out[s] = make_bars("stocks", seed=10 + k, drift=0.0002 * (k - 1))
    for k, s in enumerate(cfg["universe"]["crypto"]):
        out[s] = make_bars("crypto", seed=20 + k, drift=0.0001 * k)
    return out


@pytest.fixture
def now():
    return datetime(2026, 11, 6, 20, 35, tzinfo=timezone.utc)      # Friday 15:35 New York
