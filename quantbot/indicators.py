"""Causal technical indicators: every value uses only bars up to and including its own."""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False, min_periods=span).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder RSI."""
    d = close.diff()
    up = d.clip(lower=0.0)
    dn = (-d).clip(lower=0.0)
    avg_up = up.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    avg_dn = dn.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    rs = avg_up / avg_dn.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    out = out.where(avg_dn > 0, 100.0)          # no losses in window -> 100
    return out.where(avg_up.notna())


def true_range(df: pd.DataFrame) -> pd.Series:
    prev = df["close"].shift(1)
    return pd.concat(
        [df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()],
        axis=1,
    ).max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Wilder average true range."""
    return true_range(df).ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def safe_div(a, b):
    """a / b with zero or missing denominators giving NaN instead of inf."""
    b = b.where(b != 0) if isinstance(b, pd.Series) else b
    return a / b
