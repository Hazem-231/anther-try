"""Local dashboard: one JSON snapshot endpoint and a static page.

Binds to 127.0.0.1. The only write endpoint (run a cycle now) requires a custom
header, which browsers will not send cross-site without a CORS preflight.
"""
from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..config import STATE_DIR
from ..market_clock import NY, next_market_open, now_utc

STATIC = Path(__file__).parent / "static"


def _thin(rows: list[dict], limit: int = 600) -> list[dict]:
    if len(rows) <= limit:
        return rows
    step = len(rows) / limit
    picked = [rows[int(i * step)] for i in range(limit)]
    picked[-1] = rows[-1]
    return picked


def create_app(cfg: dict, mode: str, engine, runner=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app):
        # Start trading only once the port is ours. A second copy of the bot
        # fails to bind and exits before it can place a duplicate order.
        if runner is not None:
            runner.start()
        yield
        if runner is not None:
            runner.stop_event.set()

    app = FastAPI(title="QuantBot", docs_url=None, redoc_url=None, lifespan=lifespan)
    cache: dict = {"at": 0.0, "value": None}

    def snapshot() -> dict:
        store, broker = engine.store, engine.broker
        now = now_utc()
        out: dict = {
            "mode": mode, "now": now.isoformat(), "data_source": engine.data.name,
            "trading_enabled": runner is not None,
            "busy": bool(runner and runner.busy),
            "next_run": runner.next_run.isoformat() if runner and runner.next_run else None,
            "last_error": engine.last_error,
            "universe": cfg["universe"],
            "limits": cfg["risk"],
        }
        try:
            acct = broker.account()
            positions = broker.positions()
            out["market_open"] = broker.is_market_open(now)
        except Exception as exc:
            out["broker_error"] = str(exc)[:300]
            acct, positions = None, {}
            out["market_open"] = None
        out["next_open"] = None if out["market_open"] else next_market_open(now).isoformat()

        plans = {t["symbol"]: t for t in store.open_trades()}
        rows = []
        for s, p in positions.items():
            t = plans.get(s)
            rows.append({
                "symbol": s, "side": "long" if p["qty"] > 0 else "short", "qty": p["qty"],
                "avg_price": p["avg_price"], "price": p["price"], "market_value": p["market_value"],
                "unrealized": p["qty"] * (p["price"] - p["avg_price"]),
                "stop": t["stop"] if t else None, "target": t["target"] if t else None,
                "opened_ts": t["opened_ts"] if t else None, "managed": t is not None,
            })
        out["positions"] = rows

        eq_rows = store.query("SELECT ts, equity FROM equity ORDER BY ts")
        first = eq_rows[0]["equity"] if eq_rows else (acct["equity"] if acct else None)
        day_start = store.get(f"day_start:{now.astimezone(NY).date().isoformat()}")
        if acct:
            gross = sum(abs(p["market_value"]) for p in positions.values())
            out["account"] = {
                "equity": acct["equity"], "cash": acct["cash"], "gross_exposure": gross,
                "gross_pct": gross / acct["equity"] * 100 if acct["equity"] else 0.0,
                "start_equity": first,
                "total_pnl": acct["equity"] - first if first else 0.0,
                "day_pnl": acct["equity"] - day_start if day_start else 0.0,
                "day_pnl_pct": (acct["equity"] / day_start - 1) * 100 if day_start else 0.0,
            }
        out["equity_curve"] = _thin(eq_rows)
        out["stats"] = store.trade_stats()

        cycle = store.latest_cycle()
        out["cycle"] = cycle
        out["decisions"] = store.decisions_for(cycle["id"]) if cycle else []
        out["orders"] = store.query("SELECT * FROM orders ORDER BY id DESC LIMIT 60")
        out["closed_trades"] = store.closed_trades(60)
        return out

    @app.get("/api/state")
    def state():
        if time.time() - cache["at"] > 4 or cache["value"] is None:
            cache["value"], cache["at"] = snapshot(), time.time()
        return JSONResponse(cache["value"])

    @app.get("/api/backtest")
    def backtest():
        path = STATE_DIR / "backtest.json"
        if not path.exists():
            return JSONResponse(None)
        return JSONResponse(json.loads(path.read_text(encoding="utf-8")))

    @app.post("/api/run")
    def run_now(x_quantbot: str | None = Header(default=None)):
        if x_quantbot != "dashboard":
            raise HTTPException(403, "missing header")
        if runner is None:
            raise HTTPException(409, "started with --no-trading")
        started = runner.trigger()
        cache["at"] = 0.0
        return {"started": started}

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
