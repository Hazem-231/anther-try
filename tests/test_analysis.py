"""Indicators, signals, weight fitting and the Time Machine."""
import numpy as np
import pandas as pd

from quantbot.indicators import atr, rsi
from quantbot.signals import SIGNALS, build_panels, compute_regime, compute_signals, conviction, fit_weights
from quantbot.timemachine import _outcomes, analog_indices, simulate, verdict

from conftest import make_bars


def test_rsi_and_atr_ranges():
    df = make_bars("stocks", days=60)
    r = rsi(df["close"]).dropna()
    assert r.between(0, 100).all()
    assert (atr(df).dropna() > 0).all()
    rising = pd.Series(np.arange(1.0, 60.0))
    assert rsi(rising).iloc[-1] == 100.0


def test_signals_bounded():
    for ac in ("stocks", "crypto"):
        df = make_bars(ac, days=200)
        sig = compute_signals(df, ac, compute_regime(df, ac))
        for k in SIGNALS:
            vals = sig[k].dropna()
            assert len(vals) > 0 and vals.between(-1.0001, 1.0001).all(), k
        assert sig["vol_mult"].between(0.4, 1.0).all()


def test_signals_are_causal():
    """Cutting off the future must not change any value already computed."""
    for ac in ("stocks", "crypto"):
        df = make_bars(ac, days=200, seed=5)
        cut = len(df) - 5 * (7 if ac == "stocks" else 24) - 3
        full = compute_signals(df, ac, compute_regime(df, ac))
        part = compute_signals(df.iloc[:cut], ac, compute_regime(df.iloc[:cut], ac))
        cols = SIGNALS + ["atr", "vol_mult", "regime", "rsi"]
        a = full[cols].iloc[:cut].to_numpy(float)
        b = part[cols].to_numpy(float)
        assert np.allclose(a, b, equal_nan=True, atol=1e-9), ac


def test_regime_tilts_conviction():
    idx = pd.date_range("2026-01-01", periods=3, freq="h", tz="UTC")
    base = {k: 0.5 for k in SIGNALS}
    weights = {k: 1 / len(SIGNALS) for k in SIGNALS}
    good = pd.DataFrame({**base, "regime": 1.0}, index=idx)
    bad = pd.DataFrame({**base, "regime": -1.0}, index=idx)
    assert conviction(good, weights).iloc[0] > conviction(bad, weights).iloc[0] > 0
    short = pd.DataFrame({**{k: -0.5 for k in SIGNALS}, "regime": 1.0}, index=idx)
    assert abs(conviction(short, weights).iloc[0]) < abs(conviction(good, weights).iloc[0])


def test_fit_weights_rewards_the_predictive_signal():
    rng = np.random.default_rng(3)
    n = 3000
    idx = pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC")
    ret = rng.normal(0, 0.01, n)
    close = pd.Series(100 * np.exp(np.cumsum(ret)), index=idx)
    fwd = np.log(close.shift(-5) / close).fillna(0)
    sig = pd.DataFrame({k: rng.normal(0, 0.3, n) for k in SIGNALS}, index=idx)
    sig["trend"] = np.tanh(fwd * 30)                 # cheats: knows the future
    sig["breakout"] = -np.tanh(fwd * 30)             # anti-predictive
    prior = {k: 1 / len(SIGNALS) for k in SIGNALS}
    res = fit_weights([(sig, close)], prior, horizon=5, blend=1.0, min_ic=0.1)
    assert res["has_edge"] and res["ic"]["trend"] > 0.5 and res["ic"]["breakout"] < -0.5
    w = res["weights"]
    assert w["trend"] > 0.4 and w["breakout"] < -0.4          # the wrong-way signal is faded, not dropped
    assert w["momentum"] == 0.0 and w["volume"] == 0.0         # noise falls under min_ic
    assert abs(sum(abs(v) for v in w.values()) - 1) < 1e-9

    half = fit_weights([(sig, close)], prior, horizon=5, blend=0.5, min_ic=0.1)["weights"]
    assert half["momentum"] == 0.5 * prior["momentum"] and half["trend"] > half["momentum"]

    noise = pd.DataFrame({k: rng.normal(0, 0.3, n) for k in SIGNALS}, index=idx)
    none = fit_weights([(noise, close)], prior, 5, min_ic=0.2)
    assert not none["has_edge"] and none["weights"] == prior


def test_outcomes_stop_before_target_in_same_bar():
    cm = np.array([[1.0, 1.0]])
    hm = np.array([[1.10, 1.0]])
    lm = np.array([[0.90, 1.0]])      # first bar touches both 95 and 105
    out = _outcomes(cm, hm, lm, 100.0, 95.0, 105.0, 1, 1.0)
    assert out["p_stop"] == 1.0 and out["p_target"] == 0.0 and out["avg_r"] == -1.0
    short = _outcomes(cm, hm, lm, 100.0, 105.0, 95.0, -1, 1.0)
    assert short["p_stop"] == 1.0


def test_verdict_wording():
    assert verdict(0.5, 0.5, 1.0) == (0.0, "Close to a coin flip")
    assert verdict(0.42, 0.58, 1.5)[1] == "Close to break-even"
    assert verdict(0.6, 0.4, 1.0)[1] == "Leans toward the target"
    assert verdict(0.2, 0.8, 1.0)[1] == "Strongly toward the stop"


def _arrays(close):
    prev = np.concatenate([[np.nan], close[:-1]])
    return close / prev, close * 1.001 / prev, close * 0.999 / prev


def test_time_machine_is_repeatable_and_directional():
    n = 2000
    up = 100 * np.exp(np.cumsum(np.full(n, 0.002)))          # relentless uptrend
    cr, hr, lr = _arrays(up)
    conv = np.full(n, 0.5)
    args = dict(direction=1, entry=up[-1], stop=up[-1] * 0.98, target=up[-1] * 1.03, target_r=1.5,
                horizon=35, analog_min_abs=0.2, replays=400, seed=19)
    a = simulate(cr, hr, lr, conv, n - 1, **args)
    b = simulate(cr, hr, lr, conv, n - 1, **args)
    assert a == b                                              # fixed seed
    assert a["conditional"] and a["p_target"] > 0.95 and a["passed"]
    short = simulate(cr, hr, lr, -conv, n - 1, direction=-1, entry=up[-1], stop=up[-1] * 1.02,
                     target=up[-1] * 0.97, target_r=1.5, horizon=35, analog_min_abs=0.2, replays=400)
    assert short["p_stop"] > 0.95 and not short["passed"]


def test_time_machine_never_uses_unfinished_paths():
    conv = np.full(500, 0.5)
    idx = analog_indices(conv, t=400, direction=1, min_abs=0.2, horizon=35)
    assert idx.max() + 35 <= 400
    assert len(analog_indices(conv, 400, -1, 0.2, 35)) == 0


def test_time_machine_charges_costs():
    rng = np.random.default_rng(0)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.003, 3000)))
    cr, hr, lr = _arrays(close)
    out = simulate(cr, hr, lr, np.full(3000, 0.5), 2999, 1, close[-1], close[-1] * 0.98, close[-1] * 1.02, 1.0,
                   24, 0.2, replays=500, cost_r=5.0, min_edge_points=-100)
    assert not out["passed"] and "after costs" in out["reason"]


def test_partial_and_off_grid_bars_are_dropped():
    """Yahoo returns the forming bar plus a row stamped 'now'; neither may be used."""
    from datetime import datetime, timezone

    from quantbot.data import drop_incomplete, on_grid

    def frame(stamps):
        idx = pd.DatetimeIndex([pd.Timestamp(s, tz="UTC") for s in stamps])
        return pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}, index=idx)

    now = datetime(2026, 11, 13, 18, 8, 13, tzinfo=timezone.utc)            # 13:08 New York
    stocks = frame(["2026-11-13 15:30", "2026-11-13 16:30", "2026-11-13 17:30", "2026-11-13 18:08:11"])
    clean = drop_incomplete(on_grid(stocks, "stocks", 30), "stocks", now)
    assert list(clean.index.strftime("%H:%M")) == ["15:30", "16:30"]      # 17:30 bar is still forming

    crypto = frame(["2026-11-13 16:00", "2026-11-13 17:00", "2026-11-13 18:00", "2026-11-13 18:08:13"])
    clean = drop_incomplete(on_grid(crypto, "crypto", 30), "crypto", now)
    assert list(clean.index.strftime("%H:%M")) == ["16:00", "17:00"]

    last = frame(["2026-11-13 19:30", "2026-11-13 20:30"])                  # 15:30 bar ends at the 16:00 close
    after_close = datetime(2026, 11, 13, 21, 0, 5, tzinfo=timezone.utc)
    assert len(drop_incomplete(on_grid(last, "stocks", 30), "stocks", after_close)) == 2
    half_day = frame(["2025-12-24 17:30", "2025-12-24 18:00"])              # 13:00 closing print
    assert len(on_grid(half_day, "stocks", 30)) == 1


def test_volume_signal_goes_neutral_without_volume():
    df = make_bars("crypto", days=120)
    df.loc[df.index[::2], "volume"] = 0.0
    assert (compute_signals(df, "crypto")["volume"] == 0).all()


def test_build_panels_shapes(frames, cfg):
    stocks = {s: frames[s] for s in cfg["universe"]["stocks"]}
    panels = build_panels(stocks, "stocks", "AAA", cfg["strategy"]["weights"])
    for s, p in panels.items():
        assert len(p.conv) == len(p.df) == len(p.cr)
        assert p.sig["rel_strength"].between(-1, 1).all()
    last = [panels[s].sig["rel_strength"].iloc[-1] for s in stocks]
    assert sorted(last) == [-1.0, 0.0, 1.0]
