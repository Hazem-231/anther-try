"""The analysis layer: seven directional signals in [-1, +1], plus regime and volatility.

Everything here is causal. Daily features are shifted one session, so an hourly
bar only ever sees daily values from sessions that had already closed.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .config import BARS_PER_DAY
from .indicators import atr, ema, rsi, safe_div

SIGNALS = ["trend", "momentum", "mean_reversion", "breakout", "volume", "rel_strength", "setups"]

# name -> score contribution. Bullish ones follow Jakeschincariol/day-trader-skill;
# the bearish mirrors are our extension for the short side.
DAILY_SETUPS = {
    "breakout_20d": 0.6,
    "bull_flag": 0.5,
    "reclaim_ma20": 0.4,
    "oversold_bounce": 0.5,
    "breakdown_20d": -0.6,
    "bear_flag": -0.5,
    "lose_ma20": -0.4,
    "overbought_fade": -0.5,
}
ORB_SCORE = 0.5


def session_key(index: pd.DatetimeIndex, asset_class: str) -> pd.DatetimeIndex:
    """Trading-day label per bar: New York date for stocks, UTC date for crypto."""
    tz = "America/New_York" if asset_class == "stocks" else "UTC"
    return index.tz_convert(tz).normalize().tz_localize(None)


def daily_bars(df: pd.DataFrame, asset_class: str) -> pd.DataFrame:
    sess = session_key(df.index, asset_class)
    g = df.groupby(sess)
    return pd.DataFrame(
        {
            "open": g["open"].first(),
            "high": g["high"].max(),
            "low": g["low"].min(),
            "close": g["close"].last(),
            "volume": g["volume"].sum(),
        }
    )


def daily_setups(d: pd.DataFrame) -> pd.DataFrame:
    """Boolean setup flags on daily bars (not yet shifted)."""
    c = d["close"]
    sma20 = c.rolling(20).mean()
    r = rsi(c, 14)
    prior_hi = c.shift(1).rolling(20).max()
    prior_lo = c.shift(1).rolling(20).min()
    run = c.shift(5) / c.shift(15) - 1.0                  # 10 sessions before the last 5
    last5 = c.rolling(5)
    tight = (last5.max() - last5.min()) / last5.min() < 0.05
    out = pd.DataFrame(index=d.index)
    out["breakout_20d"] = c > prior_hi
    out["bull_flag"] = (run > 0.10) & tight
    out["reclaim_ma20"] = (c.shift(1) < sma20.shift(1)) & (c > sma20)
    out["oversold_bounce"] = (r.shift(1) < 30) & (c > c.shift(1))
    out["breakdown_20d"] = c < prior_lo
    out["bear_flag"] = (run < -0.10) & tight
    out["lose_ma20"] = (c.shift(1) > sma20.shift(1)) & (c < sma20)
    out["overbought_fade"] = (r.shift(1) > 70) & (c < c.shift(1))
    return out.fillna(False).astype(bool)


def compute_regime(bench: pd.DataFrame, asset_class: str) -> pd.Series:
    """Market regime in [-1, +1] from the benchmark's daily bars, indexed by
    session and already shifted one session (safe to map onto today's bars)."""
    d = daily_bars(bench, asset_class)
    c = d["close"]
    a = atr(d, 14)
    dret = np.log(c).diff()
    z20 = safe_div(np.log(c / c.shift(20)), dret.rolling(60).std() * np.sqrt(20))
    r = (
        0.5 * np.tanh(safe_div(c - ema(c, 50), 3 * a))
        + 0.3 * np.tanh(safe_div(ema(c, 20) - ema(c, 100), 3 * a))
        + 0.2 * np.tanh(z20 / 1.5)
    )
    return r.shift(1)


def compute_signals(df: pd.DataFrame, asset_class: str, regime: pd.Series | None = None) -> pd.DataFrame:
    """Per-bar signals for one symbol. `df` has open/high/low/close/volume on a
    tz-aware UTC index. `rel_strength` is filled in later by add_rel_strength."""
    bpd = BARS_PER_DAY[asset_class]
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"].astype(float)
    sess = session_key(df.index, asset_class)
    logret = np.log(c).diff()
    atr_h = atr(df, 14)
    out = pd.DataFrame(index=df.index)

    d = daily_bars(df, asset_class)
    dc = d["close"]
    atr_d = atr(d, 14)

    def to_hourly(daily_series: pd.Series) -> np.ndarray:
        return daily_series.shift(1).reindex(sess).to_numpy()

    # 1. Trend: hourly EMA spread plus the prior sessions' daily EMA spread.
    t_h = np.tanh(safe_div(ema(c, 3 * bpd) - ema(c, 15 * bpd), 3 * atr_h))
    t_d = to_hourly(np.tanh(safe_div(ema(dc, 10) - ema(dc, 30), 1.5 * atr_d)))
    out["trend"] = 0.5 * t_h + 0.5 * pd.Series(t_d, index=df.index)

    # 2. Momentum: volatility-normalised returns over 1, 5 and 20 days.
    sigma = logret.rolling(20 * bpd).std()
    parts = []
    for days in (1, 5, 20):
        n = days * bpd
        z = safe_div(np.log(c / c.shift(n)), sigma * np.sqrt(n))
        parts.append(np.tanh(z / 1.5))
    out["momentum"] = sum(parts) / len(parts)
    out["mom5_z"] = safe_div(np.log(c / c.shift(5 * bpd)), sigma * np.sqrt(5 * bpd))

    # 3. Mean reversion: Bollinger stretch and RSI extremes, muted in strong trends.
    z = safe_div(c - c.rolling(20).mean(), c.rolling(20).std())
    r = rsi(c, 14)
    rsi_ext = -np.sign(r - 50) * ((r - 50).abs() - 15).clip(lower=0) / 25
    mr = 0.6 * -np.tanh(z / 2) + 0.4 * rsi_ext.clip(-1, 1)
    out["mean_reversion"] = mr * (1 - out["trend"].abs())

    # 4. Breakout: where price sits in the prior 10-day high/low channel.
    n = 10 * bpd
    hi, lo = h.rolling(n).max().shift(1), l.rolling(n).min().shift(1)
    pos = safe_div(c - (hi + lo) / 2, (hi - lo) / 2).clip(-1, 1)
    out["breakout"] = pos ** 3

    # 5. Volume pressure: signed volume flow plus distance from rolling VWAP.
    n = 3 * bpd
    flow = safe_div((np.sign(logret) * v).rolling(n).sum(), v.rolling(n).sum()).fillna(0.0)
    typical = (h + l + c) / 3
    vwap = safe_div((typical * v).rolling(bpd).sum(), v.rolling(bpd).sum())
    dev = np.tanh(safe_div(c - vwap, 2 * atr_h)).fillna(0.0)
    out["volume"] = (0.6 * (flow * 2).clip(-1, 1) + 0.4 * dev).clip(-1, 1)
    if (v == 0).mean() > 0.2:          # feed has no usable volume (Yahoo crypto): stay neutral
        out["volume"] = 0.0

    # 6. Relative strength is cross-sectional; see add_rel_strength.
    out["rel_strength"] = 0.0

    # 7. Chart setups: yesterday's daily patterns, plus today's opening-range break.
    su = daily_setups(d)
    score = pd.Series(0.0, index=df.index)
    for name, weight in DAILY_SETUPS.items():
        flag = su[name].shift(1).reindex(sess).fillna(False).to_numpy().astype(bool)
        out[f"su_{name}"] = flag
        score = score + weight * flag
    if asset_class == "stocks":
        g = df.groupby(sess)
        first = g.cumcount().to_numpy() == 0
        or_hi = g["high"].transform("first").to_numpy()
        or_lo = g["low"].transform("first").to_numpy()
        up = ~first & (c.to_numpy() > or_hi)
        down = ~first & (c.to_numpy() < or_lo)
    else:
        up = down = np.zeros(len(df), dtype=bool)
    out["su_orb_up"], out["su_orb_down"] = up, down
    out["setups"] = (score + ORB_SCORE * up - ORB_SCORE * down).clip(-1, 1)

    # Risk context (not directional).
    rv_short, rv_long = logret.rolling(5 * bpd).std(), logret.rolling(60 * bpd).std()
    out["vol_mult"] = safe_div(rv_long, rv_short).clip(0.4, 1.0).fillna(0.4)
    out["atr"] = atr_h
    out["rsi"] = r
    if regime is not None:
        out["regime"] = regime.reindex(sess, method="ffill").to_numpy()
    else:
        out["regime"] = 0.0
    return out


def add_rel_strength(sigs: dict[str, pd.DataFrame]) -> None:
    """Rank each symbol's 5-day normalised return against its peers, in place."""
    if len(sigs) < 2:
        return
    wide = pd.DataFrame({s: f["mom5_z"] for s, f in sigs.items()})
    rank = wide.rank(axis=1)
    count = wide.notna().sum(axis=1)
    rs = (rank.sub(1, axis=0)).div((count - 1).where(count > 1), axis=0) * 2 - 1
    for s, f in sigs.items():
        f["rel_strength"] = rs[s].reindex(f.index).fillna(0.0)


def conviction(sig: pd.DataFrame, weights: dict[str, float], regime_strength: float = 0.5) -> pd.Series:
    """Weighted signal sum, tilted by regime: a hostile market halves a trade's
    conviction, a friendly one boosts it. NaN until the core signals warm up."""
    raw = sum(weights.get(k, 0.0) * sig[k].fillna(0.0) for k in SIGNALS)
    reg = sig["regime"].fillna(0.0)
    tilt = np.where(raw > 0, 1 + regime_strength * reg, 1 - regime_strength * reg)
    valid = sig[["trend", "momentum", "breakout"]].notna().all(axis=1)
    return (raw * tilt).clip(-1, 1).where(valid)


def active_setups(row: pd.Series) -> list[str]:
    return [k[3:] for k, val in row.items() if isinstance(k, str) and k.startswith("su_") and bool(val)]


def fit_weights(
    frames: list[tuple[pd.DataFrame, pd.Series]],
    prior: dict[str, float],
    horizon: int,
    blend: float = 1.0,
    min_ic: float = 0.01,
) -> dict:
    """Blend prior weights with weights proportional to each signal's rank
    correlation (IC) with the forward `horizon`-bar return.

    Weights are signed: a signal that has been pointing the wrong way gets a
    negative weight, i.e. the bot fades it. Correlations weaker than `min_ic`
    count as zero; if none clears it there is no edge to trade.

    `frames` is a list of (signals, close) already cut to the training window,
    so the forward return never reaches past the window's end.
    """
    xs, ys = [], []
    for sig, close in frames:
        fwd = np.log(close.shift(-horizon) / close)
        ok = fwd.notna() & sig[["trend", "momentum", "breakout"]].notna().all(axis=1)
        if ok.sum() > 0:
            xs.append(sig.loc[ok, SIGNALS].fillna(0.0))
            ys.append(fwd[ok])
    if not xs:
        return {"weights": dict(prior), "ic": {}, "has_edge": False, "n": 0}
    x, y = pd.concat(xs), pd.concat(ys)
    yr = y.rank()
    ic = {}
    for k in SIGNALS:
        col = x[k]
        ic[k] = float(np.corrcoef(col.rank(), yr)[0, 1]) if col.std() > 0 else 0.0
    kept = {k: (val if abs(val) >= min_ic else 0.0) for k, val in ic.items()}
    total = sum(abs(v) for v in kept.values())
    if total <= 0:
        return {"weights": dict(prior), "ic": ic, "has_edge": False, "n": int(len(y))}
    w = {k: (1 - blend) * prior.get(k, 0.0) + blend * kept[k] / total for k in SIGNALS}
    return {"weights": w, "ic": ic, "has_edge": True, "n": int(len(y))}


@dataclass
class SymbolPanel:
    """Everything the engine and backtester need about one symbol."""

    symbol: str
    asset_class: str
    df: pd.DataFrame
    sig: pd.DataFrame
    conv: pd.Series
    # numpy views used by the Time Machine
    close: np.ndarray
    cr: np.ndarray   # close / previous close
    hr: np.ndarray   # high / previous close
    lr: np.ndarray   # low / previous close

    def set_conviction(self, weights: dict[str, float], regime_strength: float) -> None:
        self.conv = conviction(self.sig, weights, regime_strength)


def build_panels(
    frames: dict[str, pd.DataFrame],
    asset_class: str,
    benchmark: str,
    weights: dict[str, float],
    regime_strength: float = 0.5,
) -> dict[str, SymbolPanel]:
    bench = frames.get(benchmark)
    regime = compute_regime(bench, asset_class) if bench is not None else None
    sigs = {s: compute_signals(df, asset_class, regime) for s, df in frames.items()}
    add_rel_strength(sigs)
    panels = {}
    for s, df in frames.items():
        close = df["close"].to_numpy(dtype=float)
        prev = np.concatenate([[np.nan], close[:-1]])
        panels[s] = SymbolPanel(
            symbol=s,
            asset_class=asset_class,
            df=df,
            sig=sigs[s],
            conv=conviction(sigs[s], weights, regime_strength),
            close=close,
            cr=close / prev,
            hr=df["high"].to_numpy(dtype=float) / prev,
            lr=df["low"].to_numpy(dtype=float) / prev,
        )
    return panels
