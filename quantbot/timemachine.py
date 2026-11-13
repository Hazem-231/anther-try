"""Time Machine: odds of reaching the target before the stop.

Method follows Jakeschincariol/day-trader-skill (replays stitched from past
bars, stop checked before target, fixed seed, verdict against break-even
1/(1+R)). The difference: bars are drawn from "analogs" - stretches of history
that followed a conviction reading like the current one - so the odds are
conditional on the signal rather than on the symbol alone.

Simulated from history; not a prediction.
"""
from __future__ import annotations

import numpy as np

JITTER = 2          # a replay bar is drawn from the same step of an analog path, +/- 2
BAND_PCTS = (5, 25, 50, 75, 95)


def analog_indices(conv: np.ndarray, t: int, direction: int, min_abs: float, horizon: int) -> np.ndarray:
    """Past bars whose conviction pointed the same way at least `min_abs`
    strongly, and whose full forward path had finished by bar `t`."""
    end = t - horizon
    if end <= 0:
        return np.empty(0, dtype=int)
    c = conv[: end + 1]
    with np.errstate(invalid="ignore"):
        mask = (np.sign(c) == direction) & (np.abs(c) >= min_abs)
    return np.nonzero(mask)[0]


def all_indices(conv: np.ndarray, t: int, horizon: int) -> np.ndarray:
    end = t - horizon
    if end <= 1:
        return np.empty(0, dtype=int)
    idx = np.arange(1, end + 1)
    return idx[~np.isnan(conv[idx])] if np.isnan(conv[idx]).any() else idx


def _outcomes(cm, hm, lm, entry, stop, target, direction, target_r):
    """Walk each path bar by bar. A bar touching both levels counts as a stop."""
    steps = cm.shape[1]
    closes = entry * np.cumprod(cm, axis=1)
    prev = np.concatenate([np.full((cm.shape[0], 1), entry), closes[:, :-1]], axis=1)
    highs, lows = prev * hm, prev * lm
    if direction > 0:
        stop_hit, tgt_hit = lows <= stop, highs >= target
    else:
        stop_hit, tgt_hit = highs >= stop, lows <= target
    first_stop = np.where(stop_hit.any(axis=1), stop_hit.argmax(axis=1), steps)
    first_tgt = np.where(tgt_hit.any(axis=1), tgt_hit.argmax(axis=1), steps)
    stop_first = (first_stop <= first_tgt) & (first_stop < steps)
    tgt_first = first_tgt < first_stop
    risk = abs(entry - stop)
    drift_r = direction * (closes[:, -1] - entry) / risk
    r = np.where(tgt_first, target_r, np.where(stop_first, -1.0, drift_r))
    still_open = ~(stop_first | tgt_first)
    return {
        "p_target": float(tgt_first.mean()),
        "p_stop": float(stop_first.mean()),
        "p_open": float(still_open.mean()),
        "p_green": float((tgt_first | (still_open & (drift_r > 0))).mean()),
        "avg_r": float(r.mean()),
        "closes": closes,
    }


def verdict(p_target: float, p_stop: float, target_r: float) -> tuple[float, str]:
    """Edge in percentage points over break-even, and the skill's wording."""
    decided = p_target + p_stop
    if decided <= 0:
        return 0.0, "No replay reached the stop or the target"
    edge = (p_target / decided - 1.0 / (1.0 + target_r)) * 100.0
    if abs(edge) < 5:
        return edge, "Close to a coin flip" if abs(target_r - 1.0) < 1e-9 else "Close to break-even"
    side = "the target" if edge > 0 else "the stop"
    return edge, f"{'Leans' if abs(edge) < 15 else 'Strongly'} toward {side}"


def simulate(
    cr: np.ndarray, hr: np.ndarray, lr: np.ndarray, conv: np.ndarray, t: int,
    direction: int, entry: float, stop: float, target: float, target_r: float,
    horizon: int, analog_min_abs: float, min_analogs: int = 40, replays: int = 10000,
    seed: int = 19, cost_r: float = 0.0, min_edge_points: float = 5.0, bands: bool = False,
) -> dict:
    """Run the Time Machine for a plan made at bar `t`.

    cr/hr/lr are per-bar close/high/low divided by the previous close.
    `cost_r` is the round-trip trading cost expressed in R.
    """
    idx = analog_indices(conv, t, direction, analog_min_abs, horizon)
    conditional = len(idx) >= min_analogs
    if not conditional:
        idx = all_indices(conv, t, horizon)
    if len(idx) < 20:
        return {"ok": False, "passed": False, "reason": "not enough history", "analogs": int(len(idx))}

    paths = idx[:, None] + 1 + np.arange(horizon)[None, :]
    c, h, l = cr[paths], hr[paths], lr[paths]
    hist = _outcomes(c, h, l, entry, stop, target, direction, target_r)

    rng = np.random.default_rng(seed)
    pick = rng.integers(0, len(idx), size=(replays, horizon))
    step = np.clip(np.arange(horizon)[None, :] + rng.integers(-JITTER, JITTER + 1, size=(replays, horizon)),
                   0, horizon - 1)
    sim = _outcomes(c[pick, step], h[pick, step], l[pick, step], entry, stop, target, direction, target_r)

    edge, words = verdict(sim["p_target"], sim["p_stop"], target_r)
    net_r = sim["avg_r"] - cost_r
    out = {
        "ok": True,
        "conditional": conditional,
        "analogs": int(len(idx)),
        "replays": int(replays),
        "seed": int(seed),
        "horizon_bars": int(horizon),
        "p_target": sim["p_target"],
        "p_stop": sim["p_stop"],
        "p_open": sim["p_open"],
        "p_green": sim["p_green"],
        "avg_r": sim["avg_r"],
        "cost_r": float(cost_r),
        "net_r": float(net_r),
        "breakeven": 1.0 / (1.0 + target_r),
        "edge_points": float(edge),
        "verdict": words,
        "history": {k: hist[k] for k in ("p_target", "p_stop", "p_open", "avg_r")},
        "passed": bool(edge >= min_edge_points and net_r > 0),
    }
    if not out["passed"]:
        out["reason"] = (
            f"{words.lower()} (edge {edge:+.1f} pts, needs +{min_edge_points:g})"
            if edge < min_edge_points else f"average result after costs is {net_r:+.2f}R"
        )
    if bands:
        pct = np.percentile(sim["closes"], BAND_PCTS, axis=0)
        out["bands"] = {str(p): [round(float(x), 6) for x in row] for p, row in zip(BAND_PCTS, pct)}
    return out


def next_bar_lean(conv: np.ndarray, close: np.ndarray, t: int, width: float = 0.1) -> dict:
    """What the next bar did, historically, after conviction readings within
    `width` of the current one."""
    now = conv[t]
    if t < 50 or np.isnan(now):
        return {"n": 0}
    past = conv[:t]
    with np.errstate(invalid="ignore"):
        idx = np.nonzero(np.abs(past - now) <= width)[0]
    if len(idx) < 20:
        return {"n": int(len(idx))}
    ret = close[idx + 1] / close[idx] - 1.0
    return {"n": int(len(idx)), "p_up": float((ret > 0).mean()), "avg_ret_pct": float(ret.mean() * 100)}
