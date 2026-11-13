"""Risk gate: every proposed entry gets PASS or BLOCK with a reason per check.
A BLOCK is final for that order in that cycle. Exits are never blocked."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .market_clock import in_window
from .strategy import Plan


@dataclass
class RiskContext:
    equity: float
    cash: float
    exposure: dict[str, float]          # symbol -> signed market value of open positions
    crypto_symbols: set[str]
    trades_today: int
    day_pnl_pct: float                  # today's equity change, percent
    now: datetime
    market_open: bool = True
    data_age_minutes: float = 0.0
    shortable: bool = True
    entries_enabled: bool = True        # False when the weight fit found no edge
    extra: dict = field(default_factory=dict)


def check_entry(plan: Plan, qty: float, ctx: RiskContext, cfg: dict) -> dict:
    risk, strat = cfg["risk"], cfg["strategy"]
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    notional = qty * plan.entry
    gross = sum(abs(v) for v in ctx.exposure.values())
    crypto_gross = sum(abs(v) for s, v in ctx.exposure.items() if s in ctx.crypto_symbols)
    eq = max(ctx.equity, 1e-9)

    add("kill switch", ctx.day_pnl_pct > -risk["max_daily_loss_pct"],
        f"day P&L {ctx.day_pnl_pct:+.2f}% (limit -{risk['max_daily_loss_pct']}%)")
    add("signal edge", ctx.entries_enabled,
        "weights fitted on recent data" if ctx.entries_enabled
        else "no signal had positive correlation with forward returns in the last fit")
    add("trades today", ctx.trades_today < risk["max_trades_per_day"],
        f"{ctx.trades_today} of {risk['max_trades_per_day']}")
    add("open positions", len(ctx.exposure) < risk["max_open_positions"],
        f"{len(ctx.exposure)} of {risk['max_open_positions']}")
    add("no existing position", plan.symbol not in ctx.exposure, plan.symbol)

    if plan.asset_class == "stocks":
        add("market open", ctx.market_open, "open" if ctx.market_open else "closed")
        add("entry window", in_window(ctx.now, risk.get("stock_entry_window")),
            f"{'-'.join(risk.get('stock_entry_window') or ['any', 'time'])} New York")

    if plan.side == "short":
        ok = strat.get("allow_short", False) and plan.asset_class == "stocks" and ctx.shortable
        why = ("allowed" if ok else "crypto cannot be shorted on Alpaca" if plan.asset_class == "crypto"
               else "shorting is off in config" if not strat.get("allow_short") else "not shortable at broker")
        add("short allowed", ok, why)
        add("short equity", ctx.equity >= risk["min_short_equity_usd"],
            f"equity ${ctx.equity:,.0f} (needs ${risk['min_short_equity_usd']:,})")

    add("data fresh", ctx.data_age_minutes <= risk["max_data_age_minutes"],
        f"last bar closed {ctx.data_age_minutes:.0f} min ago (limit {risk['max_data_age_minutes']})")

    d = plan.direction
    add("stop on right side", d * (plan.entry - plan.stop) > 0 and d * (plan.target - plan.entry) > 0,
        f"stop {plan.stop:.4f}, entry {plan.entry:.4f}, target {plan.target:.4f}")
    add("reward:risk", plan.target_r >= risk["min_reward_risk"],
        f"{plan.target_r:.2f} (min {risk['min_reward_risk']})")

    add("order size", qty > 0 and notional >= risk["min_order_usd"],
        f"${notional:,.2f} (min ${risk['min_order_usd']})")
    max_loss = qty * plan.risk_per_unit
    add("max loss", max_loss <= eq * risk["risk_per_trade_pct"] / 100 * 1.01,
        f"${max_loss:,.2f} at the stop ({max_loss / eq * 100:.2f}% of equity)")
    add("position size", notional <= eq * risk["max_position_pct"] / 100 * 1.001,
        f"{notional / eq * 100:.1f}% of equity (max {risk['max_position_pct']}%)")
    add("gross exposure", gross + notional <= eq * risk["max_gross_exposure_pct"] / 100 * 1.001,
        f"{(gross + notional) / eq * 100:.1f}% after fill (max {risk['max_gross_exposure_pct']}%)")
    if plan.asset_class == "crypto":
        add("crypto exposure", crypto_gross + notional <= eq * risk["max_crypto_exposure_pct"] / 100 * 1.001,
            f"{(crypto_gross + notional) / eq * 100:.1f}% after fill (max {risk['max_crypto_exposure_pct']}%)")
    if plan.side == "long":
        left = ctx.cash - notional
        add("cash buffer", left >= eq * risk["cash_buffer_pct"] / 100 * 0.999,
            f"${left:,.0f} cash left (buffer {risk['cash_buffer_pct']}%)")

    failed = [c for c in checks if not c["ok"]]
    return {
        "passed": not failed,
        "verdict": "PASS" if not failed else "BLOCK",
        "reason": "" if not failed else f"{failed[0]['name']}: {failed[0]['detail']}",
        "checks": checks,
    }
