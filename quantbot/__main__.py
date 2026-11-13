"""Command line: python -m quantbot <run|once|backtest|status>"""
from __future__ import annotations

import argparse
import sys

from .config import ConfigError, load_config, resolve_mode


def build(cfg=None):
    from .broker import make_broker
    from .data import make_data
    from .engine import Engine
    from .store import Store

    cfg = cfg or load_config()
    mode = resolve_mode()
    store = Store(mode["mode"])
    engine = Engine(cfg, store, make_data(cfg, mode), make_broker(cfg, mode, store))
    return cfg, mode, engine


def banner(mode: str) -> str:
    return {
        "dry-run": "DRY-RUN: simulated account, no broker connected.",
        "paper": "PAPER: orders go to your Alpaca paper account.",
        "live": "LIVE: orders use REAL MONEY in your Alpaca account.",
    }[mode]


def cmd_once(_args) -> None:
    cfg, mode, engine = build()
    print(banner(mode["mode"]))
    out = engine.run_cycle()
    print(f"\ncycle {out['cycle_id']} at {out['ts'][:19]}  equity ${out['equity']:,.2f}")
    print(f"{'symbol':<9}{'action':<7}{'conv':>7}  reason")
    for d in out["decisions"]:
        conv = f"{d['conviction']:+.2f}" if d["conviction"] is not None else "  n/a"
        print(f"{d['symbol']:<9}{d['action']:<7}{conv:>7}  {d['reason']}")


def cmd_backtest(args) -> None:
    from .backtest import format_report, run_backtest
    from .data import CachedData, make_data

    cfg = load_config()
    if args.no_timemachine:
        cfg["timemachine"]["enabled"] = False
    data = CachedData(make_data(cfg, resolve_mode()))
    print(format_report(run_backtest(cfg, data)))
    print("\nSaved to state/backtest.json (shown on the dashboard).")


def cmd_run(args) -> None:
    import uvicorn

    from .runner import Runner
    from .web.app import create_app

    cfg, mode, engine = build()
    print(banner(mode["mode"]))
    runner = None if args.no_trading else Runner(engine, cfg)
    app = create_app(cfg, mode["mode"], engine, runner)
    host, port = cfg["server"]["host"], args.port or cfg["server"]["port"]
    print(f"Dashboard: http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")      # the app starts the trading loop once bound


def cmd_status(_args) -> None:
    _, mode, engine = build()
    print(banner(mode["mode"]))
    acct = engine.broker.account()
    print(f"equity ${acct['equity']:,.2f}  cash ${acct['cash']:,.2f}")
    for s, p in engine.broker.positions().items():
        print(f"  {s:<9}{p['qty']:>12g} @ {p['avg_price']:.2f}  now {p['price']:.2f}")
    print(engine.store.trade_stats())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="quantbot", description="Hourly quant trading bot for Alpaca.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="start the dashboard and the hourly trading loop")
    run.add_argument("--port", type=int)
    run.add_argument("--no-trading", action="store_true", help="dashboard only; no cycles, no orders")
    run.set_defaults(fn=cmd_run)
    sub.add_parser("once", help="run a single cycle and print the decisions").set_defaults(fn=cmd_once)
    bt = sub.add_parser("backtest", help="walk-forward backtest on ~2 years of hourly bars")
    bt.add_argument("--no-timemachine", action="store_true", help="disable the Time Machine gate")
    bt.set_defaults(fn=cmd_backtest)
    sub.add_parser("status", help="show account, positions and trade stats").set_defaults(fn=cmd_status)
    args = parser.parse_args(argv)
    try:
        args.fn(args)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
