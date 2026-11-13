"""SQLite history: cycles, decisions, orders, trades, equity. One file per mode
so dry-run, paper and live records never mix."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from .config import STATE_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, equity REAL, cash REAL, summary TEXT);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id INTEGER, ts TEXT, symbol TEXT,
    asset_class TEXT, action TEXT, conviction REAL, price REAL, reason TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS decisions_cycle ON decisions (cycle_id);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, symbol TEXT, side TEXT, qty REAL,
    price REAL, status TEXT, broker_id TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, asset_class TEXT, side TEXT, qty REAL,
    entry REAL, stop REAL, target REAL, target_r REAL, opened_ts TEXT, max_hold_bars INTEGER,
    closed_ts TEXT, exit_price REAL, pnl REAL, r_multiple REAL, exit_reason TEXT);
CREATE TABLE IF NOT EXISTS equity (ts TEXT, equity REAL, cash REAL);
"""


class Store:
    def __init__(self, mode: str, path: str | Path | None = None):
        if path is None:
            STATE_DIR.mkdir(exist_ok=True)
            path = STATE_DIR / f"quantbot_{mode}.db"
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def execute(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
            return cur.lastrowid

    def query(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    # key/value state
    def get(self, key: str, default=None):
        rows = self.query("SELECT v FROM kv WHERE k = ?", (key,))
        return json.loads(rows[0]["v"]) if rows else default

    def set(self, key: str, value) -> None:
        self.execute("INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                     (key, json.dumps(value)))

    # writers
    def add_cycle(self, ts: str, equity: float, cash: float, summary: dict) -> int:
        return self.execute("INSERT INTO cycles (ts, equity, cash, summary) VALUES (?, ?, ?, ?)",
                            (ts, equity, cash, json.dumps(summary)))

    def add_decision(self, cycle_id: int, ts: str, d: dict) -> None:
        self.execute(
            "INSERT INTO decisions (cycle_id, ts, symbol, asset_class, action, conviction, price, reason, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (cycle_id, ts, d["symbol"], d["asset_class"], d["action"], d.get("conviction"),
             d.get("price"), d.get("reason", ""), json.dumps(d.get("detail", {}))),
        )

    def add_order(self, ts: str, symbol: str, side: str, qty: float, price: float | None,
                  status: str, broker_id: str, reason: str) -> None:
        self.execute(
            "INSERT INTO orders (ts, symbol, side, qty, price, status, broker_id, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, symbol, side, qty, price, status, broker_id, reason),
        )

    def open_trade(self, ts: str, plan: dict, qty: float) -> int:
        return self.execute(
            "INSERT INTO trades (symbol, asset_class, side, qty, entry, stop, target, target_r, opened_ts, max_hold_bars) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (plan["symbol"], plan["asset_class"], plan["side"], qty, plan["entry"], plan["stop"],
             plan["target"], plan["target_r"], ts, plan["max_hold_bars"]),
        )

    def close_trade(self, trade_id: int, ts: str, exit_price: float, pnl: float, r: float, reason: str) -> None:
        self.execute(
            "UPDATE trades SET closed_ts = ?, exit_price = ?, pnl = ?, r_multiple = ?, exit_reason = ? WHERE id = ?",
            (ts, exit_price, pnl, r, reason, trade_id),
        )

    def add_equity(self, ts: str, equity: float, cash: float) -> None:
        self.execute("INSERT INTO equity (ts, equity, cash) VALUES (?, ?, ?)", (ts, equity, cash))

    # readers
    def open_trades(self) -> list[dict]:
        return self.query("SELECT * FROM trades WHERE closed_ts IS NULL ORDER BY opened_ts")

    def closed_trades(self, limit: int = 200) -> list[dict]:
        return self.query("SELECT * FROM trades WHERE closed_ts IS NOT NULL ORDER BY closed_ts DESC LIMIT ?", (limit,))

    def latest_cycle(self) -> dict | None:
        rows = self.query("SELECT * FROM cycles ORDER BY id DESC LIMIT 1")
        if not rows:
            return None
        rows[0]["summary"] = json.loads(rows[0]["summary"] or "{}")
        return rows[0]

    def decisions_for(self, cycle_id: int) -> list[dict]:
        rows = self.query("SELECT * FROM decisions WHERE cycle_id = ? ORDER BY id", (cycle_id,))
        for r in rows:
            r["detail"] = json.loads(r["detail"] or "{}")
        return rows

    def trade_stats(self) -> dict:
        rows = self.query("SELECT pnl, r_multiple FROM trades WHERE closed_ts IS NOT NULL")
        n = len(rows)
        if not n:
            return {"trades": 0}
        wins = [r for r in rows if (r["pnl"] or 0) > 0]
        gain = sum(r["pnl"] for r in wins)
        loss = -sum(r["pnl"] for r in rows if (r["pnl"] or 0) < 0)
        return {
            "trades": n,
            "win_rate": len(wins) / n,
            "avg_r": sum(r["r_multiple"] or 0 for r in rows) / n,
            "profit_factor": (gain / loss) if loss > 0 else None,
            "total_pnl": sum(r["pnl"] or 0 for r in rows),
        }
