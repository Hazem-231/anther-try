"""Order execution. DryRunBroker simulates fills locally; AlpacaBroker talks to
Alpaca (paper unless the user enabled live in .env themselves).

Shared interface:
    account()   -> {"equity", "cash", "buying_power"}
    positions() -> {symbol: {"qty" (signed), "avg_price", "price", "market_value"}}
    submit(symbol, asset_class, side, qty, ref_price) -> fill dict
    close(symbol, asset_class, ref_price)             -> fill dict
    is_market_open(now), shortable(symbol)
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime

from . import market_clock


def _fill(status: str, side: str, qty: float, price: float | None, order_id: str, fee: float = 0.0, error: str = "") -> dict:
    return {"status": status, "side": side, "qty": qty, "price": price, "id": order_id, "fee": fee, "error": error}


class DryRunBroker:
    """A simulated account kept in the local database. Fills at the reference
    price plus slippage; crypto pays the configured fee."""

    mode = "dry-run"

    def __init__(self, store, cfg: dict):
        self.store = store
        self.costs = cfg["costs"]
        self.crypto = set(cfg["universe"]["crypto"])
        if store.get("dry:cash") is None:
            store.set("dry:cash", float(cfg["dry_run"]["starting_cash"]))
            store.set("dry:positions", {})
            store.set("dry:marks", {})

    def _state(self):
        return self.store.get("dry:cash"), self.store.get("dry:positions", {}), self.store.get("dry:marks", {})

    def mark(self, symbol: str, price: float) -> None:
        marks = self.store.get("dry:marks", {})
        marks[symbol] = float(price)
        self.store.set("dry:marks", marks)

    def positions(self) -> dict:
        _, pos, marks = self._state()
        out = {}
        for s, p in pos.items():
            price = marks.get(s, p["avg_price"])
            out[s] = {"qty": p["qty"], "avg_price": p["avg_price"], "price": price, "market_value": p["qty"] * price}
        return out

    def account(self) -> dict:
        cash, _, _ = self._state()
        equity = cash + sum(p["market_value"] for p in self.positions().values())
        return {"equity": equity, "cash": cash, "buying_power": max(cash, 0.0)}

    def submit(self, symbol: str, asset_class: str, side: str, qty: float, ref_price: float) -> dict:
        if qty <= 0 or ref_price <= 0:
            return _fill("rejected", side, qty, None, "", error="bad quantity or price")
        crypto = asset_class == "crypto"
        slip = (self.costs["crypto_slippage_bps"] if crypto else self.costs["stock_slippage_bps"]) / 1e4
        sign = 1 if side == "buy" else -1
        price = ref_price * (1 + sign * slip)
        fee = qty * price * self.costs["crypto_fee_bps"] / 1e4 if crypto else 0.0
        cash, pos, _ = self._state()
        cur = pos.get(symbol, {"qty": 0.0, "avg_price": 0.0})
        new_qty = cur["qty"] + sign * qty
        if abs(new_qty) < 1e-9:
            pos.pop(symbol, None)
        else:
            adding = cur["qty"] == 0 or (cur["qty"] > 0) == (sign > 0)
            avg = ((abs(cur["qty"]) * cur["avg_price"] + qty * price) / abs(new_qty)) if adding else cur["avg_price"]
            pos[symbol] = {"qty": new_qty, "avg_price": avg}
        self.store.set("dry:cash", cash - sign * qty * price - fee)
        self.store.set("dry:positions", pos)
        self.mark(symbol, ref_price)
        return _fill("filled", side, qty, price, f"dry-{uuid.uuid4().hex[:10]}", fee)

    def close(self, symbol: str, asset_class: str, ref_price: float) -> dict:
        cur = self.store.get("dry:positions", {}).get(symbol)
        if not cur:
            return _fill("rejected", "sell", 0.0, None, "", error="no position")
        return self.submit(symbol, asset_class, "sell" if cur["qty"] > 0 else "buy", abs(cur["qty"]), ref_price)

    def is_market_open(self, now: datetime | None = None) -> bool:
        return market_clock.is_market_open(now)

    def shortable(self, symbol: str) -> bool:
        return symbol not in self.crypto


class AlpacaBroker:
    def __init__(self, key: str, secret: str, paper: bool, cfg: dict):
        from alpaca.trading.client import TradingClient

        self.client = TradingClient(key, secret, paper=paper)
        self.mode = "paper" if paper else "live"
        self.crypto = set(cfg["universe"]["crypto"])
        self._alias = {s.replace("/", ""): s for s in self.crypto}

    def account(self) -> dict:
        a = self.client.get_account()
        return {"equity": float(a.equity), "cash": float(a.cash), "buying_power": float(a.buying_power)}

    def positions(self) -> dict:
        out = {}
        for p in self.client.get_all_positions():
            symbol = self._alias.get(p.symbol, p.symbol)
            sign = -1 if str(getattr(p.side, "value", p.side)).lower() == "short" else 1
            qty = sign * abs(float(p.qty))
            price = float(p.current_price) if p.current_price is not None else float(p.avg_entry_price)
            out[symbol] = {"qty": qty, "avg_price": float(p.avg_entry_price), "price": price, "market_value": qty * price}
        return out

    def _await_fill(self, order, side: str, qty: float, wait: float = 12.0) -> dict:
        deadline = time.time() + wait
        while True:
            status = str(getattr(order.status, "value", order.status)).lower()
            if status == "filled":
                return _fill("filled", side, float(order.filled_qty), float(order.filled_avg_price), str(order.id))
            if status in ("canceled", "expired", "rejected"):
                return _fill(status, side, qty, None, str(order.id), error=status)
            if time.time() >= deadline:
                return _fill("submitted", side, qty, None, str(order.id))
            time.sleep(1.0)
            order = self.client.get_order_by_id(order.id)

    def submit(self, symbol: str, asset_class: str, side: str, qty: float, ref_price: float) -> dict:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        try:
            order = self.client.submit_order(MarketOrderRequest(
                symbol=symbol, qty=qty,
                side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                time_in_force=TimeInForce.GTC if asset_class == "crypto" else TimeInForce.DAY,
            ))
        except Exception as exc:                      # broker said no; record it, don't crash the cycle
            return _fill("rejected", side, qty, None, "", error=str(exc)[:300])
        return self._await_fill(order, side, qty)

    def close(self, symbol: str, asset_class: str, ref_price: float) -> dict:
        held = self.positions().get(symbol)
        if not held:
            return _fill("rejected", "sell", 0.0, None, "", error="no position")
        side = "sell" if held["qty"] > 0 else "buy"
        try:
            order = self.client.close_position(symbol.replace("/", ""))
        except Exception as exc:
            return _fill("rejected", side, abs(held["qty"]), None, "", error=str(exc)[:300])
        return self._await_fill(order, side, abs(held["qty"]))

    def is_market_open(self, now: datetime | None = None) -> bool:
        return bool(self.client.get_clock().is_open)

    def shortable(self, symbol: str) -> bool:
        if symbol in self.crypto:
            return False
        try:
            asset = self.client.get_asset(symbol)
            return bool(asset.shortable and asset.easy_to_borrow)
        except Exception:
            return False


def make_broker(cfg: dict, mode: dict, store):
    if mode["mode"] == "dry-run":
        return DryRunBroker(store, cfg)
    return AlpacaBroker(mode["key"], mode["secret"], paper=(mode["mode"] == "paper"), cfg=cfg)
