"""Trade plans, sizing and exit rules. Pure functions shared by the live engine
and the backtester so both make the same decisions."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from .config import BARS_PER_DAY


@dataclass
class Plan:
    symbol: str
    asset_class: str
    side: str            # "long" | "short"
    entry: float
    stop: float
    target: float
    target_r: float
    max_hold_bars: int

    @property
    def direction(self) -> int:
        return 1 if self.side == "long" else -1

    @property
    def risk_per_unit(self) -> float:
        return abs(self.entry - self.stop)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["risk_per_unit"] = self.risk_per_unit
        d["risk_pct"] = self.risk_per_unit / self.entry * 100
        return d

    def reanchor(self, fill: float) -> "Plan":
        """Keep the stop/target distances but measure them from the real fill."""
        shift = fill - self.entry
        return Plan(self.symbol, self.asset_class, self.side, fill, self.stop + shift,
                    self.target + shift, self.target_r, self.max_hold_bars)


def build_plan(symbol: str, asset_class: str, direction: int, price: float, atr: float, params: dict) -> Plan | None:
    """ATR stop, R-multiple target. Returns None when ATR is unusable."""
    if not (atr and atr > 0 and price > 0) or math.isnan(atr):
        return None
    risk = params["atr_stop_mult"] * atr
    if risk < price * 0.0005:                      # stop tighter than 0.05% is noise
        return None
    r = params["target_r"]
    stop = price - direction * risk
    target = price + direction * r * risk
    if stop <= 0 or target <= 0:
        return None
    return Plan(symbol, asset_class, "long" if direction > 0 else "short", price, stop, target, r,
                int(params["max_hold_days"] * BARS_PER_DAY[asset_class]))


def round_qty(qty: float, asset_class: str, side: str) -> float:
    """Alpaca: shorts must be whole shares; long stock and crypto may be fractional."""
    if qty <= 0:
        return 0.0
    if asset_class == "crypto":
        return math.floor(qty * 1e6) / 1e6
    if side == "short":
        return float(math.floor(qty))
    return float(math.floor(qty)) if qty >= 1 else math.floor(qty * 1e4) / 1e4


def size_position(plan: Plan, equity: float, cash: float, vol_mult: float, risk_cfg: dict) -> float:
    """Quantity = max loss per trade / risk per unit, then capped by position
    size and (for longs) by cash after the buffer."""
    budget = equity * risk_cfg["risk_per_trade_pct"] / 100.0 * vol_mult
    qty = budget / plan.risk_per_unit
    qty = min(qty, equity * risk_cfg["max_position_pct"] / 100.0 / plan.entry)
    if plan.side == "long":
        spendable = cash - equity * risk_cfg["cash_buffer_pct"] / 100.0
        qty = min(qty, max(spendable, 0.0) / plan.entry)
    qty = round_qty(qty, plan.asset_class, plan.side)
    if qty * plan.entry < risk_cfg["min_order_usd"]:
        return 0.0
    return qty


def round_trip_cost_r(plan: Plan, costs: dict) -> float:
    """Slippage and fees for entering and leaving, as a fraction of risk (R)."""
    if plan.asset_class == "crypto":
        bps = 2 * (costs["crypto_slippage_bps"] + costs["crypto_fee_bps"])
    else:
        bps = 2 * costs["stock_slippage_bps"]
    return plan.entry * bps / 1e4 / plan.risk_per_unit


def exit_reason(side: str, price: float, stop: float, target: float, conviction: float | None,
                bars_held: int, max_hold_bars: int, exit_threshold: float) -> str | None:
    """Why an open trade should close now, or None to keep holding."""
    d = 1 if side == "long" else -1
    if d * (price - stop) <= 0:
        return "stop hit"
    if d * (price - target) >= 0:
        return "target hit"
    if conviction is not None and not math.isnan(conviction) and d * conviction <= -exit_threshold:
        return "signal reversed"
    if bars_held >= max_hold_bars:
        return "time stop"
    return None


def lean_label(conviction: float | None) -> str:
    if conviction is None or math.isnan(conviction):
        return "No reading"
    a = abs(conviction)
    side = "bullish" if conviction > 0 else "bearish"
    if a < 0.08:
        return "Neutral"
    if a < 0.2:
        return f"Slightly {side}"
    if a < 0.4:
        return side.capitalize()
    return f"Strongly {side}"
