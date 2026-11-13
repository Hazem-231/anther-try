"""Walk-forward backtest using the same signal, plan, sizing, risk and Time
Machine code as the live engine.

Honesty rules:
  * weights are fitted only on bars before the block they trade;
  * a decision made at a bar's close fills at the next bar's open;
  * inside a bar the stop is checked before the target;
  * slippage on every fill, fees on crypto;
  * nothing trades during the warm-up period.
Not modelled: short borrow fees, partial fills, halts, early closes.
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from .config import BARS_PER_DAY, STATE_DIR
from .market_clock import NY
from .risk import RiskContext, check_entry
from .signals import build_panels, fit_weights
from .strategy import build_plan, exit_reason, round_trip_cost_r, size_position
from .timemachine import simulate

NS_HOUR = 3_600_000_000_000


def _stats(eq: pd.Series, trades: pd.DataFrame) -> dict:
    daily = eq.resample("1D").last().dropna()
    rets = daily.pct_change().dropna()
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    total = eq.iloc[-1] / eq.iloc[0] - 1
    out = {
        "total_return_pct": total * 100,
        "cagr_pct": ((1 + total) ** (1 / years) - 1) * 100 if total > -1 else -100.0,
        "sharpe": float(rets.mean() / rets.std() * math.sqrt(365)) if rets.std() > 0 else 0.0,
        "max_drawdown_pct": float((eq / eq.cummax() - 1).min() * 100),
        "trades": int(len(trades)),
    }
    out.update(_trade_stats(trades))
    return out


def _trade_stats(tr: pd.DataFrame) -> dict:
    if tr.empty:
        return {"win_rate_pct": None, "avg_r": None, "profit_factor": None, "expectancy_usd": None, "avg_hold_bars": None}
    wins, losses = tr[tr["pnl"] > 0], tr[tr["pnl"] < 0]
    gross_loss = -losses["pnl"].sum()
    return {
        "win_rate_pct": float(len(wins) / len(tr) * 100),
        "avg_r": float(tr["r"].mean()),
        "profit_factor": float(wins["pnl"].sum() / gross_loss) if gross_loss > 0 else None,
        "expectancy_usd": float(tr["pnl"].mean()),
        "avg_hold_bars": float(tr["bars"].mean()),
        "net_pnl_usd": float(tr["pnl"].sum()),
    }


def run_backtest(cfg: dict, data, log=print, save: bool = True) -> dict:
    bt, strat, costs, tm_cfg, fit_cfg = cfg["backtest"], cfg["strategy"], cfg["costs"], cfg["timemachine"], cfg["strategy"]["fit"]
    prior = dict(strat["weights"])
    started = time.time()

    panels, cls = {}, {}
    for ac in ("stocks", "crypto"):
        frames = {}
        if hasattr(data, "preload"):
            try:
                data.preload(cfg["universe"][ac], ac, bt["days"])
            except Exception as exc:
                log(f"  batch load failed for {ac}: {exc}")
        for s in cfg["universe"][ac]:
            try:
                frames[s] = data.bars(s, ac, bt["days"])
            except Exception as exc:
                log(f"  skipped {s}: {exc}")
        if frames:
            panels.update(build_panels(frames, ac, cfg["benchmarks"][ac], prior, strat["regime_strength"]))
            cls.update({s: ac for s in frames})
        log(f"loaded {len(frames)} {ac} symbols")
    if not panels:
        raise RuntimeError("no data loaded")

    symbols = list(panels)
    A = {}
    for s, p in panels.items():
        A[s] = {
            "ns": p.df.index.asi8 if p.df.index.unit == "ns" else p.df.index.as_unit("ns").asi8,
            "o": p.df["open"].to_numpy(float), "h": p.df["high"].to_numpy(float),
            "l": p.df["low"].to_numpy(float), "c": p.close,
            "atr": p.sig["atr"].to_numpy(float), "vm": p.sig["vol_mult"].to_numpy(float),
            "conv": p.conv.to_numpy(float),
        }
    timeline = np.unique(np.concatenate([a["ns"] for a in A.values()]))
    start_ns = timeline[0] + bt["warmup_days"] * 24 * NS_HOUR
    if start_ns >= timeline[-1]:
        raise RuntimeError("not enough history for the warm-up period")

    slip = {"stocks": costs["stock_slippage_bps"] / 1e4, "crypto": costs["crypto_slippage_bps"] / 1e4}
    fee = {"stocks": 0.0, "crypto": costs["crypto_fee_bps"] / 1e4}
    crypto_set = set(cfg["universe"]["crypto"])

    cash = float(cfg["dry_run"]["starting_cash"])
    start_cash = cash
    positions: dict[str, dict] = {}
    pending: dict[str, tuple] = {}
    cooldown_until: dict[str, int] = {}
    ptr = {s: 0 for s in symbols}
    last_close: dict[str, float] = {}
    weights = {ac: dict(prior) for ac in ("stocks", "crypto")}
    has_edge = {"stocks": True, "crypto": True}
    next_refit = {"stocks": start_ns, "crypto": start_ns}
    fit_log, trades, eq_ns, eq_val, gross_val = [], [], [], [], []
    gate = {"candidates": 0, "time_machine_blocked": 0, "risk_blocked": 0, "entered": 0, "risk_reasons": {},
            "long": {"candidates": 0, "time_machine_blocked": 0, "risk_blocked": 0},
            "short": {"candidates": 0, "time_machine_blocked": 0, "risk_blocked": 0}}
    day_key, day_start_eq, trades_today = None, cash, 0

    def equity_now() -> float:
        return cash + sum(p["dir"] * p["qty"] * last_close.get(s, p["entry"]) for s, p in positions.items())

    def close_position(s: str, price: float, ns: int, i: int, reason: str) -> None:
        nonlocal cash
        p = positions.pop(s)
        ac = cls[s]
        fill = price * (1 - p["dir"] * slip[ac])
        cost = p["qty"] * fill * fee[ac]
        cash += p["dir"] * p["qty"] * fill - cost
        if reason == "stop hit":
            cooldown_until[s] = ns + int(strat.get("cooldown_hours", 0)) * NS_HOUR
        pnl = p["dir"] * (fill - p["entry"]) * p["qty"] - cost - p["entry_fee"]
        trades.append({
            "symbol": s, "asset_class": ac, "side": "long" if p["dir"] > 0 else "short", "qty": p["qty"],
            "entry": p["entry"], "exit": fill, "pnl": pnl, "r": p["dir"] * (fill - p["entry"]) / p["risk"],
            "bars": i - p["i"], "reason": reason, "opened": int(p["ns"]), "closed": int(ns),
        })

    for step, ns in enumerate(timeline):
        trading = ns >= start_ns
        now = datetime.fromtimestamp(ns / 1e9, tz=timezone.utc)

        if trading:
            for ac in ("stocks", "crypto"):
                if ns < next_refit[ac] or not fit_cfg.get("enabled", True):
                    continue
                lo_ns = ns - fit_cfg["window_days"] * 24 * NS_HOUR
                frames = []
                for s in symbols:
                    if cls[s] != ac:
                        continue
                    a, p = A[s], panels[s]
                    lo, hi = int(np.searchsorted(a["ns"], lo_ns)), ptr[s]      # bars strictly before now
                    if hi - lo > 50:
                        frames.append((p.sig.iloc[lo:hi], p.df["close"].iloc[lo:hi]))
                res = fit_weights(frames, prior, fit_cfg["horizon_days"] * BARS_PER_DAY[ac], fit_cfg["blend"],
                                  fit_cfg.get("min_ic", 0.01))
                weights[ac], has_edge[ac] = res["weights"], res["has_edge"]
                for s in symbols:
                    if cls[s] == ac:
                        panels[s].set_conviction(res["weights"], strat["regime_strength"])
                        A[s]["conv"] = panels[s].conv.to_numpy(float)
                fit_log.append({"ts": now.isoformat(), "asset_class": ac, "has_edge": res["has_edge"],
                                "weights": {k: round(v, 3) for k, v in res["weights"].items()},
                                "ic": {k: round(v, 4) for k, v in res["ic"].items()}})
                next_refit[ac] = ns + bt["refit_days"] * 24 * NS_HOUR

            ny_day = now.astimezone(NY).date()
            if ny_day != day_key:
                day_key, day_start_eq, trades_today = ny_day, equity_now(), 0

        for s in symbols:
            a = A[s]
            i = ptr[s]
            if i >= len(a["ns"]) or a["ns"][i] != ns:
                continue
            ptr[s] = i + 1
            ac, params = cls[s], strat[cls[s]]
            o, h, l, c = a["o"][i], a["h"][i], a["l"][i], a["c"][i]

            # (a) fill what was decided at the previous close, at this open
            if s in pending:
                kind, payload = pending.pop(s)
                if kind == "exit" and s in positions:
                    close_position(s, o, ns, i, payload)
                elif kind == "enter" and s not in positions:
                    plan, qty = payload
                    d = plan.direction
                    fill = o * (1 + d * slip[ac])
                    live = plan.reanchor(fill)
                    cost = qty * fill * fee[ac]
                    if d < 0 or cash - qty * fill - cost >= 0:
                        cash -= d * qty * fill + cost
                        positions[s] = {"dir": d, "qty": qty, "entry": fill, "stop": live.stop, "target": live.target,
                                        "risk": live.risk_per_unit, "i": i, "ns": ns, "max_hold": plan.max_hold_bars,
                                        "entry_fee": cost}
                        gate["entered"] += 1
                        trades_today += 1

            # (b) stop / target inside this bar, stop first
            if s in positions:
                p = positions[s]
                if p["dir"] > 0:
                    if l <= p["stop"]:
                        close_position(s, min(o, p["stop"]), ns, i, "stop hit")
                    elif h >= p["target"]:
                        close_position(s, p["target"], ns, i, "target hit")
                else:
                    if h >= p["stop"]:
                        close_position(s, max(o, p["stop"]), ns, i, "stop hit")
                    elif l <= p["target"]:
                        close_position(s, p["target"], ns, i, "target hit")

            last_close[s] = c
            if not trading or i + 1 >= len(a["ns"]):
                continue
            conv = a["conv"][i]

            # (c) decisions at the close
            if s in positions:
                p = positions[s]
                why = exit_reason("long" if p["dir"] > 0 else "short", c, p["stop"], p["target"], conv,
                                  i - p["i"] + 1, p["max_hold"], params["exit_threshold"])
                if why in ("signal reversed", "time stop"):
                    pending[s] = ("exit", why)
                continue
            if math.isnan(conv) or abs(conv) < params["entry_threshold"] or ns < cooldown_until.get(s, 0):
                continue
            d = 1 if conv > 0 else -1
            if d < 0 and (ac == "crypto" or not strat["allow_short"]):
                continue
            plan = build_plan(s, ac, d, c, a["atr"][i], params)
            if plan is None:
                continue
            gate["candidates"] += 1
            gate[plan.side]["candidates"] += 1
            equity = equity_now()
            qty = size_position(plan, equity, cash, a["vm"][i], cfg["risk"])
            bar_end = now + timedelta(hours=1)
            if ac == "stocks":
                bar_end = min(bar_end, now.astimezone(NY).replace(hour=16, minute=0).astimezone(timezone.utc))
            ctx = RiskContext(
                equity=equity, cash=cash,
                exposure={k: v["dir"] * v["qty"] * last_close.get(k, v["entry"]) for k, v in positions.items()},
                crypto_symbols=crypto_set, trades_today=trades_today,
                day_pnl_pct=(equity / day_start_eq - 1) * 100 if day_start_eq else 0.0,
                now=bar_end, market_open=True, data_age_minutes=0.0, shortable=True,
                entries_enabled=has_edge[ac] or not fit_cfg.get("require_edge", True),
            )
            verdict = check_entry(plan, qty, ctx, cfg)
            if not verdict["passed"]:
                gate["risk_blocked"] += 1
                gate[plan.side]["risk_blocked"] += 1
                why = verdict["reason"].split(":")[0]
                gate["risk_reasons"][why] = gate["risk_reasons"].get(why, 0) + 1
                continue
            if tm_cfg.get("enabled", True):
                pn = panels[s]
                tm = simulate(pn.cr, pn.hr, pn.lr, a["conv"], i, d, plan.entry, plan.stop, plan.target,
                              plan.target_r, plan.max_hold_bars, params["entry_threshold"] * tm_cfg["analog_strength"],
                              tm_cfg["min_analogs"], bt["replays"], tm_cfg["seed"], round_trip_cost_r(plan, costs),
                              tm_cfg["min_edge_points"])
                if not tm.get("passed"):
                    gate["time_machine_blocked"] += 1
                    gate[plan.side]["time_machine_blocked"] += 1
                    continue
            pending[s] = ("enter", (plan, qty))

        if trading:
            eq_ns.append(ns)
            eq_val.append(equity_now())
            gross_val.append(sum(p["qty"] * last_close.get(s, p["entry"]) for s, p in positions.items()))
        if step % 4000 == 0 and step:
            log(f"  {step}/{len(timeline)} bars, {len(trades)} trades")

    # close whatever is still open at the last price, so the totals are realised
    for s in list(positions):
        close_position(s, last_close[s], timeline[-1], len(A[s]["ns"]) - 1, "end of test")

    eq = pd.Series(eq_val, index=pd.to_datetime(eq_ns, utc=True))
    eq.iloc[-1] = cash
    tr = pd.DataFrame(trades)
    result = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "data_source": data.name,
        "period": {"start": eq.index[0].isoformat(), "end": eq.index[-1].isoformat(),
                   "history_start": pd.Timestamp(timeline[0], tz="UTC").isoformat()},
        "starting_cash": start_cash,
        "overall": _stats(eq, tr),
        "avg_gross_exposure_pct": float(np.mean(np.array(gross_val) / np.array(eq_val)) * 100),
        "gate": gate,
        "by_class": {}, "by_side": {}, "by_exit": {},
        "benchmarks": {},
        "fits": fit_log,
        "params": {"strategy": strat, "risk": cfg["risk"], "costs": costs, "timemachine": tm_cfg, "backtest": bt},
        "seconds": None,
    }
    if not tr.empty:
        for col, key in (("asset_class", "by_class"), ("side", "by_side"), ("reason", "by_exit")):
            for name, grp in tr.groupby(col):
                result[key][name] = {"trades": int(len(grp)), **_trade_stats(grp)}

    daily = eq.resample("1D").last().dropna()
    curve = {"dates": [d.strftime("%Y-%m-%d") for d in daily.index],
             "strategy": [round(float(v / start_cash * 100), 3) for v in daily]}
    for label, sym in (("SPY buy & hold", cfg["benchmarks"]["stocks"]), ("BTC buy & hold", cfg["benchmarks"]["crypto"])):
        if sym in panels:
            px = panels[sym].df["close"]
            px = px[px.index >= eq.index[0]]
            if len(px) > 1:
                b = px.resample("1D").last().reindex(daily.index, method="ffill").bfill()
                beq = b / px.iloc[0]
                bret = beq.pct_change().dropna()
                result["benchmarks"][label] = {
                    "total_return_pct": float((px.iloc[-1] / px.iloc[0] - 1) * 100),
                    "max_drawdown_pct": float((beq / beq.cummax() - 1).min() * 100),
                    "sharpe": float(bret.mean() / bret.std() * math.sqrt(365)) if bret.std() > 0 else 0.0,
                }
                curve[label] = [round(float(v * 100), 3) for v in beq]
    result["curve"] = curve
    result["trades_sample"] = [
        {**t, "opened": pd.Timestamp(t["opened"], tz="UTC").isoformat(), "closed": pd.Timestamp(t["closed"], tz="UTC").isoformat(),
         "pnl": round(t["pnl"], 2), "r": round(t["r"], 3)} for t in trades[-200:]
    ]
    result["seconds"] = round(time.time() - started, 1)
    if save:
        STATE_DIR.mkdir(exist_ok=True)
        (STATE_DIR / "backtest.json").write_text(json.dumps(result, indent=1, default=float), encoding="utf-8")
    return result


def format_report(r: dict) -> str:
    o = r["overall"]

    def n(x, fmt="{:.2f}"):
        return "n/a" if x is None else fmt.format(x)

    lines = [
        f"Walk-forward backtest ({r['data_source']} data)",
        f"  traded {r['period']['start'][:10]} to {r['period']['end'][:10]} "
        f"(history from {r['period']['history_start'][:10]} used for warm-up)",
        "",
        f"  Total return   {o['total_return_pct']:+.2f}%   (CAGR {o['cagr_pct']:+.2f}%)",
        f"  Sharpe         {o['sharpe']:.2f}",
        f"  Max drawdown   {o['max_drawdown_pct']:.2f}%",
        f"  Trades         {o['trades']}   win rate {n(o['win_rate_pct'], '{:.1f}')}%   "
        f"avg {n(o['avg_r'])}R   profit factor {n(o['profit_factor'])}",
        f"  Avg exposure   {r['avg_gross_exposure_pct']:.1f}% of equity",
        f"  Gate           {r['gate']['candidates']} candidates, {r['gate']['time_machine_blocked']} blocked by "
        f"Time Machine, {r['gate']['risk_blocked']} by risk, {r['gate']['entered']} entered",
        f"    long  {r['gate']['long']}",
        f"    short {r['gate']['short']}",
        f"    risk blocks: {r['gate']['risk_reasons']}",
        "",
    ]
    for title, key in (("By asset class", "by_class"), ("By side", "by_side"), ("By exit", "by_exit")):
        lines.append(f"  {title}:")
        for name, s in r[key].items():
            lines.append(f"    {name:<16} {s['trades']:>4} trades  win {n(s['win_rate_pct'], '{:.1f}')}%  "
                         f"avg {n(s['avg_r'])}R  net ${n(s.get('net_pnl_usd'), '{:,.0f}')}")
    lines.append("")
    for name, b in r["benchmarks"].items():
        lines.append(f"  {name:<16} {b['total_return_pct']:+.2f}%  Sharpe {b['sharpe']:.2f}  max DD {b['max_drawdown_pct']:.2f}%")
    lines.append(f"\n  ({r['seconds']}s)")
    return "\n".join(lines)
