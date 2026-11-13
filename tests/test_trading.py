"""Plans, sizing, risk gate, brokers, mode guard, engine cycle, backtest."""
from datetime import datetime, timezone

import pytest

from quantbot.backtest import run_backtest
from quantbot.broker import DryRunBroker
from quantbot.config import LIVE_PHRASE, ConfigError, resolve_mode
from quantbot.engine import Engine
from quantbot.risk import RiskContext, check_entry
from quantbot.store import Store
from quantbot.strategy import build_plan, exit_reason, round_qty, size_position

from conftest import FakeData

STOCK = {"entry_threshold": 0.3, "exit_threshold": 0.1, "atr_stop_mult": 2.0, "target_r": 1.5, "max_hold_days": 5}


def ctx(**kw):
    base = dict(equity=100_000.0, cash=100_000.0, exposure={}, crypto_symbols={"XXX/USD"}, trades_today=0,
                day_pnl_pct=0.0, now=datetime(2026, 11, 6, 16, 0, tzinfo=timezone.utc))      # 11:00 New York
    base.update(kw)
    return RiskContext(**base)


def failed(result):
    return [c["name"] for c in result["checks"] if not c["ok"]]


# ---- plans and sizing -------------------------------------------------------
def test_plan_levels():
    long = build_plan("AAA", "stocks", 1, 100.0, 1.0, STOCK)
    assert (long.stop, long.target, long.max_hold_bars) == (98.0, 103.0, 35)
    short = build_plan("AAA", "stocks", -1, 100.0, 1.0, STOCK)
    assert (short.stop, short.target, short.side) == (102.0, 97.0, "short")
    assert build_plan("AAA", "stocks", 1, 100.0, 0.0, STOCK) is None
    assert build_plan("AAA", "stocks", 1, 100.0, float("nan"), STOCK) is None
    moved = long.reanchor(100.5)
    assert (moved.stop, moved.target) == (98.5, 103.5)


def test_size_is_max_loss_over_risk(cfg):
    plan = build_plan("AAA", "stocks", 1, 100.0, 1.0, STOCK)                # $2 risk per share
    qty = size_position(plan, 100_000, 100_000, 1.0, cfg["risk"])
    assert qty == 100                                                        # 10% position cap binds (250 uncapped)
    wide = build_plan("AAA", "stocks", 1, 100.0, 5.0, STOCK)                # $10 risk per share
    assert size_position(wide, 100_000, 100_000, 1.0, cfg["risk"]) == 50    # $500 / $10
    assert size_position(wide, 100_000, 100_000, 0.5, cfg["risk"]) == 25    # high volatility halves it
    assert size_position(wide, 100_000, 5_000, 1.0, cfg["risk"]) == 0       # cash is inside the buffer


def test_round_qty_rules():
    assert round_qty(12.7, "stocks", "short") == 12
    assert round_qty(0.7, "stocks", "short") == 0
    assert round_qty(0.73219, "stocks", "long") == 0.7321
    assert round_qty(0.123456789, "crypto", "long") == 0.123456


def test_exit_rules():
    assert exit_reason("long", 97.9, 98, 103, 0.5, 1, 35, 0.1) == "stop hit"
    assert exit_reason("long", 103.1, 98, 103, 0.5, 1, 35, 0.1) == "target hit"
    assert exit_reason("long", 100, 98, 103, -0.2, 1, 35, 0.1) == "signal reversed"
    assert exit_reason("long", 100, 98, 103, 0.2, 35, 35, 0.1) == "time stop"
    assert exit_reason("long", 100, 98, 103, 0.2, 3, 35, 0.1) is None
    assert exit_reason("short", 102.5, 102, 97, -0.5, 1, 35, 0.1) == "stop hit"
    assert exit_reason("short", 96.0, 102, 97, -0.5, 1, 35, 0.1) == "target hit"


# ---- risk gate --------------------------------------------------------------
def test_risk_pass_and_blocks(cfg):
    plan = build_plan("AAA", "stocks", 1, 100.0, 5.0, STOCK)
    assert check_entry(plan, 50, ctx(), cfg)["verdict"] == "PASS"
    assert failed(check_entry(plan, 50, ctx(day_pnl_pct=-2.5), cfg)) == ["kill switch"]
    assert failed(check_entry(plan, 50, ctx(trades_today=8), cfg)) == ["trades today"]
    assert failed(check_entry(plan, 50, ctx(market_open=False), cfg)) == ["market open"]
    assert failed(check_entry(plan, 50, ctx(entries_enabled=False), cfg)) == ["signal edge"]
    assert failed(check_entry(plan, 50, ctx(data_age_minutes=400), cfg)) == ["data fresh"]
    late = datetime(2026, 11, 6, 20, 45, tzinfo=timezone.utc)               # 15:45 New York
    assert failed(check_entry(plan, 50, ctx(now=late), cfg)) == ["entry window"]
    assert "max loss" in failed(check_entry(plan, 80, ctx(), cfg))
    assert "position size" in failed(check_entry(plan, 150, ctx(), cfg))
    full = {"BBB": 50_000.0, "CCC": -48_000.0}
    assert "gross exposure" in failed(check_entry(plan, 50, ctx(exposure=full), cfg))
    assert "cash buffer" in failed(check_entry(plan, 50, ctx(cash=9_000.0), cfg))
    assert failed(check_entry(plan, 50, ctx(exposure={"AAA": 1000.0}), cfg)) == ["no existing position"]


def test_risk_shorts(cfg):
    short = build_plan("AAA", "stocks", -1, 100.0, 5.0, STOCK)
    assert check_entry(short, 50, ctx(), cfg)["passed"]
    assert failed(check_entry(short, 50, ctx(shortable=False), cfg)) == ["short allowed"]
    assert "short equity" in failed(check_entry(short, 1, ctx(equity=1500.0, cash=1500.0), cfg))
    crypto = build_plan("XXX/USD", "crypto", -1, 100.0, 5.0, STOCK)
    blocked = check_entry(crypto, 5, ctx(), cfg)
    assert "short allowed" in failed(blocked) and "crypto cannot be shorted" in blocked["reason"]
    cfg["strategy"]["allow_short"] = False
    assert failed(check_entry(short, 50, ctx(), cfg)) == ["short allowed"]


# ---- mode guard -------------------------------------------------------------
def test_mode_resolution():
    assert resolve_mode({})["mode"] == "dry-run"
    keys = {"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s"}
    assert resolve_mode(keys)["mode"] == "paper"
    assert resolve_mode({**keys, "ALPACA_PAPER": "true"})["mode"] == "paper"
    with pytest.raises(ConfigError):
        resolve_mode({**keys, "ALPACA_PAPER": "false"})
    with pytest.raises(ConfigError):
        resolve_mode({**keys, "ALPACA_PAPER": "false", "LIVE_TRADING_CONFIRM": "yes"})
    assert resolve_mode({**keys, "ALPACA_PAPER": "false", "LIVE_TRADING_CONFIRM": LIVE_PHRASE})["mode"] == "live"


# ---- dry-run broker ---------------------------------------------------------
def test_dry_run_round_trips(cfg, tmp_path):
    store = Store("dry-run", tmp_path / "t.db")
    b = DryRunBroker(store, cfg)
    assert b.account()["equity"] == 100_000

    buy = b.submit("AAA", "stocks", "buy", 100, 50.0)
    assert buy["status"] == "filled" and buy["price"] == pytest.approx(50.0 * 1.0002)
    b.mark("AAA", 55.0)
    assert b.positions()["AAA"]["qty"] == 100
    assert b.account()["equity"] == pytest.approx(100_000 + 100 * (55.0 - 50.01))
    b.close("AAA", "stocks", 55.0)
    assert b.positions() == {}
    assert b.account()["cash"] == pytest.approx(100_000 + 100 * (55.0 * 0.9998 - 50.01))

    start = b.account()["cash"]
    b.submit("BBB", "stocks", "sell", 10, 200.0)                           # short
    assert b.positions()["BBB"]["qty"] == -10
    b.mark("BBB", 190.0)
    assert b.account()["equity"] == pytest.approx(start + 10 * (200.0 * 0.9998 - 190.0))
    b.close("BBB", "stocks", 190.0)
    assert b.account()["cash"] > start

    before = b.account()["cash"]
    b.submit("XXX/USD", "crypto", "buy", 1, 1000.0)
    b.close("XXX/USD", "crypto", 1000.0)
    assert before - b.account()["cash"] == pytest.approx(1000.5 * 0.0025 + 999.5 * 0.0025 + 1.0)   # fees + slippage
    assert b.submit("AAA", "stocks", "buy", 0, 50.0)["status"] == "rejected"
    assert Store("dry-run", tmp_path / "t.db").get("dry:cash") == pytest.approx(b.account()["cash"])  # persisted


# ---- engine -----------------------------------------------------------------
def make_engine(cfg, frames, tmp_path):
    store = Store("dry-run", tmp_path / "e.db")
    return Engine(cfg, store, FakeData(frames), DryRunBroker(store, cfg)), store


def test_cycle_records_a_decision_for_every_symbol(cfg, frames, now, tmp_path):
    engine, store = make_engine(cfg, frames, tmp_path)
    out = engine.run_cycle(now)
    symbols = cfg["universe"]["stocks"] + cfg["universe"]["crypto"]
    assert [d["symbol"] for d in out["decisions"]] == symbols
    assert {d["action"] for d in out["decisions"]} <= {"BUY", "SHORT", "SELL", "COVER", "HOLD", "WAIT", "SKIP"}
    saved = store.decisions_for(out["cycle_id"])
    assert len(saved) == len(symbols) and all("signals" in d["detail"] for d in saved)
    assert set(out["summary"]["fits"]) == {"stocks", "crypto"}
    assert store.get("weights:stocks")["fitted_ts"] == now.isoformat()
    assert len(store.query("SELECT * FROM equity")) == 1
    for d in out["decisions"]:                                             # crypto is never shorted
        assert not (d["asset_class"] == "crypto" and d["action"] == "SHORT")


def test_forced_entry_then_stop_exit(cfg, frames, now, tmp_path):
    """Lower the bar so trades happen, then crash the price through the stop."""
    for ac in ("stocks", "crypto"):
        cfg["strategy"][ac]["entry_threshold"] = 0.01
    cfg["timemachine"]["enabled"] = False
    cfg["strategy"]["fit"]["require_edge"] = False
    cfg["risk"]["max_data_age_minutes"] = 10_000
    engine, store = make_engine(cfg, frames, tmp_path)
    out = engine.run_cycle(now)
    opened = store.open_trades()
    assert opened, [d["reason"] for d in out["decisions"]]
    assert len(store.query("SELECT * FROM orders")) == len(opened)
    for tr in opened:
        risk = tr["qty"] * abs(tr["entry"] - tr["stop"])
        assert risk <= 100_000 * 0.005 * 1.02                              # never more than 0.5% at the stop
        assert not (tr["asset_class"] == "crypto" and tr["side"] == "short")

    victim = opened[0]
    d = 1 if victim["side"] == "long" else -1
    engine.data.last_price[victim["symbol"]] = victim["stop"] - d * 0.5
    closed = engine.check_exits(now)
    assert [c["symbol"] for c in closed] == [victim["symbol"]] and closed[0]["reason"] == "stop hit"
    assert closed[0]["pnl"] < 0 and closed[0]["r_multiple"] < -0.9
    assert victim["symbol"] not in engine.broker.positions()
    assert store.trade_stats()["trades"] == 1

    held = {t["symbol"] for t in store.open_trades()}
    again = {dd["symbol"]: dd for dd in engine.run_cycle(now)["decisions"]}
    assert all(again[s]["action"] in ("HOLD", "SELL", "COVER") for s in held)   # held trades are managed, not re-entered
    assert again[victim["symbol"]]["action"] == "WAIT"                          # no re-entry right after a stop
    assert "Cooling down" in again[victim["symbol"]]["reason"]


def test_kill_switch_stops_new_entries(cfg, frames, now, tmp_path):
    for ac in ("stocks", "crypto"):
        cfg["strategy"][ac]["entry_threshold"] = 0.01
    cfg["timemachine"]["enabled"] = False
    cfg["strategy"]["fit"]["require_edge"] = False
    cfg["risk"]["max_data_age_minutes"] = 10_000
    engine, store = make_engine(cfg, frames, tmp_path)
    store.set("day_start:2026-11-06", 110_000.0)                           # account is down 9% today
    out = engine.run_cycle(now)
    assert out["summary"]["kill_switch"] and not store.open_trades()
    assert any("kill switch" in d["reason"] for d in out["decisions"])


# ---- backtest ---------------------------------------------------------------
def test_backtest_runs_and_adds_up(cfg, frames):
    for ac in ("stocks", "crypto"):
        cfg["strategy"][ac]["entry_threshold"] = 0.1
    cfg["strategy"]["fit"]["require_edge"] = False
    cfg["timemachine"]["min_edge_points"] = -100
    r = run_backtest(cfg, FakeData(frames), log=lambda *_: None, save=False)
    o = r["overall"]
    assert o["trades"] > 5 and r["gate"]["entered"] == o["trades"]
    net = sum(s["net_pnl_usd"] for s in r["by_class"].values())
    assert net == pytest.approx(r["starting_cash"] * o["total_return_pct"] / 100, abs=0.5)   # cash reconciles with trades
    assert r["period"]["start"] > r["period"]["history_start"]
    assert len(r["curve"]["dates"]) == len(r["curve"]["strategy"])
    assert o["max_drawdown_pct"] <= 0
