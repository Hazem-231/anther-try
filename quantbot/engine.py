"""The hourly cycle: data -> signals -> conviction -> exits -> entries -> records."""
from __future__ import annotations

import math
import threading
import time
import traceback
from datetime import datetime, timedelta

import pandas as pd

from .config import BARS_PER_DAY
from .market_clock import NY, now_utc
from .risk import RiskContext, check_entry
from .signals import SIGNALS, SymbolPanel, active_setups, build_panels, fit_weights
from .strategy import build_plan, exit_reason, lean_label, round_trip_cost_r, size_position
from .timemachine import next_bar_lean, simulate

CLASSES = ("stocks", "crypto")


def _f(x, nd: int = 4):
    """JSON-safe rounded float."""
    if x is None:
        return None
    x = float(x)
    return None if math.isnan(x) or math.isinf(x) else round(x, nd)


class Engine:
    def __init__(self, cfg: dict, store, data, broker):
        self.cfg, self.store, self.data, self.broker = cfg, store, data, broker
        self.lock = threading.Lock()
        self.crypto = set(cfg["universe"]["crypto"])
        self.last_error: str | None = None

    # ---- weights -----------------------------------------------------------
    def weights_for(self, ac: str) -> dict:
        saved = self.store.get(f"weights:{ac}")
        if saved:
            return saved
        return {"weights": dict(self.cfg["strategy"]["weights"]), "ic": {}, "has_edge": True, "fitted_ts": None}

    def _maybe_refit(self, ac: str, panels: dict[str, SymbolPanel], now: datetime, current: dict) -> dict:
        fit = self.cfg["strategy"]["fit"]
        if not fit.get("enabled", True):
            return current
        fitted = current.get("fitted_ts")
        if fitted and now - datetime.fromisoformat(fitted) < timedelta(days=fit["refit_days"]):
            return current
        cut = pd.Timestamp(now - timedelta(days=fit["window_days"]))
        frames = [(p.sig.loc[p.sig.index >= cut], p.df["close"].loc[p.df.index >= cut]) for p in panels.values()]
        res = fit_weights(frames, self.cfg["strategy"]["weights"], fit["horizon_days"] * BARS_PER_DAY[ac],
                          fit["blend"], fit.get("min_ic", 0.01))
        res["fitted_ts"] = now.isoformat()
        self.store.set(f"weights:{ac}", res)
        for p in panels.values():
            p.set_conviction(res["weights"], self.cfg["strategy"]["regime_strength"])
        return res

    # ---- helpers -----------------------------------------------------------
    def _trades_today(self, now: datetime) -> int:
        midnight = now.astimezone(NY).replace(hour=0, minute=0, second=0, microsecond=0)
        rows = self.store.query("SELECT COUNT(*) AS n FROM trades WHERE opened_ts >= ?",
                                (midnight.astimezone(now.tzinfo).isoformat(),))
        return rows[0]["n"]

    def _day_pnl_pct(self, now: datetime, equity: float) -> float:
        key = f"day_start:{now.astimezone(NY).date().isoformat()}"
        start = self.store.get(key)
        if start is None:
            self.store.set(key, equity)
            return 0.0
        return (equity / start - 1.0) * 100.0 if start else 0.0

    def _record_order(self, ts: str, symbol: str, fill: dict, reason: str) -> None:
        self.store.add_order(ts, symbol, fill["side"], fill["qty"], fill["price"], fill["status"],
                             fill["id"], reason if not fill["error"] else f"{reason} | {fill['error']}")

    def _close_trade(self, trade: dict, ts: str, fill: dict, ref_price: float, reason: str) -> dict:
        price = fill["price"] or ref_price
        d = 1 if trade["side"] == "long" else -1
        pnl = d * (price - trade["entry"]) * trade["qty"] - fill.get("fee", 0.0)
        risk = abs(trade["entry"] - trade["stop"]) or 1e-9
        r = d * (price - trade["entry"]) / risk
        self.store.close_trade(trade["id"], ts, price, pnl, r, reason)
        return {"exit_price": _f(price), "pnl": _f(pnl, 2), "r_multiple": _f(r, 2)}

    def _snapshot(self, panel: SymbolPanel, thr: float, weights: dict) -> dict:
        """The analysis shown on the dashboard for one symbol's latest bar."""
        t = len(panel.df) - 1
        row = panel.sig.iloc[t]
        conv = panel.conv.iloc[t]
        return {
            "bar_ts": panel.df.index[t].isoformat(),
            "signals": {k: _f(row[k], 3) for k in SIGNALS},
            "weights": {k: _f(weights.get(k, 0.0), 3) for k in SIGNALS},
            "regime": _f(row["regime"], 3),
            "vol_mult": _f(row["vol_mult"], 2),
            "rsi": _f(row["rsi"], 1),
            "atr": _f(row["atr"], 4),
            "setups": active_setups(row),
            "lean": lean_label(conv),
            "next_bar": next_bar_lean(panel.conv.to_numpy(), panel.close, t),
            "threshold": thr,
        }

    # ---- the cycle ---------------------------------------------------------
    def run_cycle(self, now: datetime | None = None) -> dict:
        with self.lock:
            try:
                out = self._run_cycle(now or now_utc())
                self.last_error = None
                return out
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                traceback.print_exc()
                raise

    def _run_cycle(self, now: datetime) -> dict:
        cfg, strat = self.cfg, self.cfg["strategy"]
        ts = now.isoformat()
        market_open = self.broker.is_market_open(now)
        began = time.time()

        def log(msg: str) -> None:
            print(f"  [{time.time() - began:5.1f}s] {msg}", flush=True)

        # 1. data and analysis
        panels: dict[str, SymbolPanel] = {}
        fits: dict[str, dict] = {}
        errors: dict[str, str] = {}
        for ac in CLASSES:
            frames = {}
            if hasattr(self.data, "preload"):
                try:                                     # one batched request per asset class
                    self.data.preload(cfg["universe"][ac], ac, cfg["data"]["lookback_days"], now)
                except Exception as exc:                 # fall back to symbol-by-symbol below
                    log(f"{ac}: batch load failed ({str(exc)[:120]})")
            for s in cfg["universe"][ac]:
                try:
                    df = self.data.bars(s, ac, cfg["data"]["lookback_days"], now)
                    if len(df) < 30 * BARS_PER_DAY[ac]:
                        raise ValueError(f"only {len(df)} bars")
                    frames[s] = df
                except Exception as exc:
                    errors[s] = str(exc)[:200]
            log(f"{ac}: loaded {len(frames)} symbols")
            if not frames:
                continue
            fit = self.weights_for(ac)
            p = build_panels(frames, ac, cfg["benchmarks"][ac], fit["weights"], strat["regime_strength"])
            fits[ac] = self._maybe_refit(ac, p, now, fit)
            panels.update(p)
            log(f"{ac}: signals and weights ready")

        price = {s: float(self.data.last_price.get(s) or p.close[-1]) for s, p in panels.items()}
        if hasattr(self.broker, "mark"):
            for s, px in price.items():
                self.broker.mark(s, px)

        decisions: dict[str, dict] = {}
        positions = self.broker.positions()

        # 2. exits for trades the bot opened
        for trade in self.store.open_trades():
            s, ac = trade["symbol"], trade["asset_class"]
            pos, panel = positions.get(s), panels.get(s)
            px = (pos or {}).get("price") or price.get(s) or trade["entry"]
            if not pos or abs(pos["qty"]) < 1e-9:
                res = self._close_trade(trade, ts, {"price": px, "fee": 0.0}, px, "closed outside the bot")
                decisions[s] = self._decision(s, ac, "HOLD", None, px, "Position was closed outside the bot.",
                                              {"closed": res})
                continue
            conv = float(panel.conv.iloc[-1]) if panel is not None else None
            held = int((panel.df.index > pd.Timestamp(trade["opened_ts"])).sum()) if panel is not None else 0
            params = strat[ac]
            why = exit_reason(trade["side"], px, trade["stop"], trade["target"], conv, held,
                              trade["max_hold_bars"], params["exit_threshold"])
            detail = self._snapshot(panel, params["entry_threshold"], fits[ac]["weights"]) if panel is not None else {}
            detail["position"] = {
                "side": trade["side"], "qty": trade["qty"], "entry": _f(trade["entry"]), "stop": _f(trade["stop"]),
                "target": _f(trade["target"]), "bars_held": held, "max_hold_bars": trade["max_hold_bars"],
                "unrealized": _f((1 if trade["side"] == "long" else -1) * (px - trade["entry"]) * trade["qty"], 2),
            }
            tradable = ac == "crypto" or market_open
            if why and tradable:
                fill = self.broker.close(s, ac, px)
                action = "SELL" if trade["side"] == "long" else "COVER"
                self._record_order(ts, s, fill, f"exit: {why}")
                if fill["status"] in ("filled", "submitted"):
                    detail["closed"] = self._close_trade(trade, ts, fill, px, why)
                    decisions[s] = self._decision(s, ac, action, conv, px, f"Closed: {why}.", detail)
                else:
                    decisions[s] = self._decision(s, ac, "HOLD", conv, px,
                                                  f"Exit ({why}) was rejected: {fill['error']}", detail)
            else:
                left = trade["max_hold_bars"] - held
                detail["trigger"] = (
                    f"Closes at stop {trade['stop']:.2f} or target {trade['target']:.2f}, if conviction "
                    f"reverses past {params['exit_threshold']:.2f}, or after {max(left, 0)} more bars."
                )
                msg = f"Exit due ({why}) but the market is closed." if why else "Holding; no exit rule triggered."
                decisions[s] = self._decision(s, ac, "HOLD", conv, px, msg, detail)

        # 3. entries
        log("exits checked")
        positions = self.broker.positions()
        acct = self.broker.account()
        equity, cash = acct["equity"], acct["cash"]
        exposure = {s: p["market_value"] for s, p in positions.items()}
        trades_today = self._trades_today(now)
        day_pnl = self._day_pnl_pct(now, equity)
        tm_cfg = cfg["timemachine"]
        since = (now - timedelta(hours=strat.get("cooldown_hours", 0))).isoformat()
        cooling = {r["symbol"] for r in self.store.query(
            "SELECT symbol FROM trades WHERE exit_reason = 'stop hit' AND closed_ts > ?", (since,))}

        order = sorted((s for s in panels if s not in decisions),
                       key=lambda s: -abs(float(panels[s].conv.iloc[-1])) if not math.isnan(panels[s].conv.iloc[-1]) else 0)
        for s in order:
            panel = panels[s]
            ac, params = panel.asset_class, strat[panel.asset_class]
            thr = params["entry_threshold"]
            conv = float(panel.conv.iloc[-1])
            px = price[s]
            detail = self._snapshot(panel, thr, fits[ac]["weights"])
            t = len(panel.df) - 1

            if s in positions:
                decisions[s] = self._decision(s, ac, "HOLD", conv, px,
                                              "Position not opened by the bot; left alone.", detail)
                continue
            if math.isnan(conv):
                decisions[s] = self._decision(s, ac, "WAIT", None, px, "Not enough history yet.", detail)
                continue
            if abs(conv) < thr:
                detail["trigger"] = (f"Buys above {thr:+.2f}" + ("" if ac == "crypto" or not strat["allow_short"]
                                     else f", shorts below {-thr:+.2f}") + f" (now {conv:+.2f}).")
                decisions[s] = self._decision(s, ac, "WAIT", conv, px, "Conviction below the entry threshold.", detail)
                continue

            if s in cooling:
                detail["trigger"] = f"Stop was hit recently; eligible again after {strat['cooldown_hours']}h."
                decisions[s] = self._decision(s, ac, "WAIT", conv, px, "Cooling down after a stop.", detail)
                continue

            direction = 1 if conv > 0 else -1
            if direction < 0 and ac == "crypto":
                detail["trigger"] = f"Bearish, but Alpaca does not allow crypto shorts. Buys above {thr:+.2f}."
                decisions[s] = self._decision(s, ac, "WAIT", conv, px, "Bearish; crypto is long-only.", detail)
                continue

            plan = build_plan(s, ac, direction, px, float(panel.sig["atr"].iloc[t]), params)
            if plan is None:
                decisions[s] = self._decision(s, ac, "SKIP", conv, px, "Could not build a plan (no usable ATR).", detail)
                continue
            detail["plan"] = {k: _f(v) if isinstance(v, float) else v for k, v in plan.to_dict().items()}

            if tm_cfg.get("enabled", True):
                tm = simulate(
                    panel.cr, panel.hr, panel.lr, panel.conv.to_numpy(), t, direction, plan.entry, plan.stop,
                    plan.target, plan.target_r, plan.max_hold_bars, thr * tm_cfg["analog_strength"],
                    tm_cfg["min_analogs"], tm_cfg["replays"], tm_cfg["seed"], round_trip_cost_r(plan, cfg["costs"]),
                    tm_cfg["min_edge_points"], bands=True,
                )
            else:
                tm = {"ok": True, "passed": True, "verdict": "Time Machine is switched off"}
            detail["timemachine"] = tm

            qty = size_position(plan, equity, cash, float(panel.sig["vol_mult"].iloc[t]), cfg["risk"])
            detail["qty"] = qty
            bar_end = panel.df.index[t].to_pydatetime() + timedelta(hours=1)
            ctx = RiskContext(
                equity=equity, cash=cash, exposure=exposure, crypto_symbols=self.crypto,
                trades_today=trades_today, day_pnl_pct=day_pnl, now=now, market_open=market_open,
                data_age_minutes=max((now - bar_end).total_seconds() / 60.0, 0.0),
                shortable=self.broker.shortable(s) if direction < 0 else True,
                entries_enabled=fits[ac].get("has_edge", True) or not strat["fit"].get("require_edge", True),
            )
            gate = check_entry(plan, qty, ctx, cfg)
            detail["risk"] = gate
            want = "BUY" if direction > 0 else "SHORT"

            if not tm.get("passed"):
                decisions[s] = self._decision(s, ac, "SKIP", conv, px,
                                              f"Wanted to {want}. Time Machine: {tm.get('reason', 'failed')}.", detail)
                continue
            if not gate["passed"]:
                decisions[s] = self._decision(s, ac, "SKIP", conv, px,
                                              f"Wanted to {want}. Risk gate BLOCK - {gate['reason']}.", detail)
                continue

            fill = self.broker.submit(s, ac, "buy" if direction > 0 else "sell", qty, px)
            self._record_order(ts, s, fill, f"entry: conviction {conv:+.2f}")
            if fill["status"] not in ("filled", "submitted"):
                decisions[s] = self._decision(s, ac, "SKIP", conv, px, f"Order rejected: {fill['error']}", detail)
                continue
            live_plan = plan.reanchor(fill["price"] or px)
            filled_qty = fill["qty"] or qty
            self.store.open_trade(ts, live_plan.to_dict(), filled_qty)
            detail["plan"] = {k: _f(v) if isinstance(v, float) else v for k, v in live_plan.to_dict().items()}
            notional = filled_qty * live_plan.entry
            exposure[s] = direction * notional
            cash -= direction * notional
            trades_today += 1
            decisions[s] = self._decision(
                s, ac, want, conv, px,
                f"{'Bought' if direction > 0 else 'Shorted'} {filled_qty:g} at {live_plan.entry:.2f}; "
                f"stop {live_plan.stop:.2f}, target {live_plan.target:.2f}.", detail)

        for s, err in errors.items():
            ac = "crypto" if s in self.crypto else "stocks"
            decisions[s] = self._decision(s, ac, "ERROR", None, None, f"No data: {err}", {})

        # 4. records
        log("entries checked")
        acct = self.broker.account()
        counts: dict[str, int] = {}
        for d in decisions.values():
            counts[d["action"]] = counts.get(d["action"], 0) + 1
        regime = {}
        for ac in CLASSES:
            bench = panels.get(cfg["benchmarks"][ac])
            if bench is not None:
                regime[ac] = _f(bench.sig["regime"].iloc[-1], 3)
        summary = {
            "market_open": market_open, "counts": counts, "errors": errors, "regime": regime,
            "day_pnl_pct": _f(day_pnl, 3), "kill_switch": day_pnl <= -cfg["risk"]["max_daily_loss_pct"],
            "fits": {ac: {"weights": {k: _f(v, 3) for k, v in f["weights"].items()},
                          "ic": {k: _f(v, 4) for k, v in f.get("ic", {}).items()},
                          "has_edge": f.get("has_edge", True), "fitted_ts": f.get("fitted_ts")}
                     for ac, f in fits.items()},
            "data_source": self.data.name,
        }
        cycle_id = self.store.add_cycle(ts, acct["equity"], acct["cash"], summary)
        universe = cfg["universe"]["stocks"] + cfg["universe"]["crypto"]
        for s in universe:
            if s in decisions:
                self.store.add_decision(cycle_id, ts, decisions[s])
        self.store.add_equity(ts, acct["equity"], acct["cash"])
        return {"cycle_id": cycle_id, "ts": ts, "equity": acct["equity"], "summary": summary,
                "decisions": [decisions[s] for s in universe if s in decisions]}

    @staticmethod
    def _decision(symbol, ac, action, conv, px, reason, detail) -> dict:
        return {"symbol": symbol, "asset_class": ac, "action": action, "conviction": _f(conv, 3),
                "price": _f(px), "reason": reason, "detail": detail}

    # ---- fast loop: stops and targets only ---------------------------------
    def check_exits(self, now: datetime | None = None) -> list[dict]:
        if not self.lock.acquire(blocking=False):
            return []
        try:
            now = now or now_utc()
            ts = now.isoformat()
            trades = self.store.open_trades()
            if not trades:
                return []
            market_open = self.broker.is_market_open(now)
            closed = []
            if hasattr(self.broker, "mark"):
                for tr in trades:
                    if tr["asset_class"] == "crypto" or market_open:
                        try:
                            self.broker.mark(tr["symbol"], self.data.latest_price(tr["symbol"], tr["asset_class"]))
                        except Exception:
                            pass
            positions = self.broker.positions()
            for tr in trades:
                if tr["asset_class"] == "stocks" and not market_open:
                    continue
                pos = positions.get(tr["symbol"])
                if not pos:
                    continue
                why = exit_reason(tr["side"], pos["price"], tr["stop"], tr["target"], None, 0, 1, 1.0)
                if why:
                    fill = self.broker.close(tr["symbol"], tr["asset_class"], pos["price"])
                    self._record_order(ts, tr["symbol"], fill, f"exit: {why}")
                    if fill["status"] in ("filled", "submitted"):
                        res = self._close_trade(tr, ts, fill, pos["price"], why)
                        closed.append({"symbol": tr["symbol"], "reason": why, **res})
            if closed:
                acct = self.broker.account()
                self.store.add_equity(ts, acct["equity"], acct["cash"])
            return closed
        finally:
            self.lock.release()
