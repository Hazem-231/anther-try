# QuantBot

An hourly quant trading bot for Alpaca with a local dashboard. It scores a fixed
list of US stocks/ETFs and crypto every hour, decides buy / short / sell / hold,
places the orders, and shows its full reasoning at http://127.0.0.1:8765.

**This is not financial advice. The backtest shows a small edge at best, well
below simply holding SPY. Run it on paper until you have seen enough to judge it.**

## Start

Double-click `start.bat`, or:

```
.venv\Scripts\python.exe -m quantbot run
```

Then open http://127.0.0.1:8765. Leave the window open; closing it stops the bot.

| Command | What it does |
|---|---|
| `python -m quantbot run` | dashboard + hourly trading loop |
| `python -m quantbot run --no-trading` | dashboard only, no cycles, no orders |
| `python -m quantbot once` | one analysis cycle, printed to the terminal |
| `python -m quantbot backtest` | walk-forward backtest, saved for the dashboard |
| `python -m quantbot status` | account, positions, trade stats |
| `python -m pytest` | offline test suite |

## Modes

| Mode | When | Orders go to |
|---|---|---|
| DRY-RUN | no keys in `.env` | a simulated $100,000 account on this PC (Yahoo data) |
| PAPER | keys in `.env`, `ALPACA_PAPER=true` | your Alpaca paper account |
| LIVE | keys, `ALPACA_PAPER=false` and `LIVE_TRADING_CONFIRM=I-ACCEPT-THE-RISK` | your real Alpaca account |

To connect Alpaca: copy `.env.example` to `.env` and paste your **paper** API key
and secret into it yourself. Never paste keys into a chat. Each mode keeps its
own history file in `state/`.

Tested against an Alpaca paper account on 2026-11-13: account, positions,
clock, shortability, stock and crypto bars, and full analysis cycles. No signal
passed both gates during that test, so order submission and fills have not been
exercised against Alpaca yet. Check the first orders in the dashboard's Orders
table and in Alpaca's own paper dashboard.

With Alpaca data the first analysis of each day downloads 400 days of hourly
bars and takes about 4-5 minutes. Later cycles fetch only new bars and take a
few seconds. The history is kept in `data_cache/` so restarts are quick.

## How it decides

Every hour, per symbol:

1. **Seven signals**, each from -1 (bearish) to +1 (bullish): trend, momentum,
   mean reversion, breakout, volume pressure, relative strength, chart setups.
2. **Conviction** = weighted sum, tilted by the market regime (SPY for stocks,
   BTC for crypto). Weights are refitted weekly from each signal's rank
   correlation with the next two days' return over the last 270 days. A signal
   that has been pointing the wrong way gets a negative weight: the bot fades it.
3. **Open trades** close on stop, target, a conviction reversal, or a time stop.
   Stops and targets are also checked every 5 minutes.
4. **New trades** need conviction past the entry threshold, then:
   - a plan: ATR-based stop, target at a multiple of the risk;
   - the **Time Machine**: 10,000 replays stitched from past bars that followed
     a similar reading; skipped unless the odds beat break-even by 5 points and
     the average result is positive after costs;
   - sizing: quantity = 0.5% of the account / (entry - stop), capped at 10%;
   - the **risk gate**: PASS or BLOCK with a reason per check (kill switch at
     -2% on the day, max trades, exposure caps, cash buffer, entry window,
     fresh data, shortability).

Stocks can be bought or shorted; crypto is long-only because Alpaca does not
allow crypto shorts. All limits live in `config.yaml`.

Setups, the Time Machine method, the PASS/BLOCK risk gate and max-loss sizing
follow [Jakeschincariol/day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill)
(MIT), re-implemented here. That skill requires a human yes for every order;
this bot trades unattended, which is why paper is the default.

## Backtest (run 2026-11-13, Yahoo hourly bars)

Traded 2025-05-13 to 2026-11-13 after a 180-day warm-up, $100,000 start,
slippage and crypto fees charged, weights fitted only on earlier data.

| | QuantBot | SPY buy & hold |
|---|---|---|
| Total return | +5.5% | +27.9% |
| Sharpe | 0.52 | 1.36 |
| Max drawdown | -5.2% | -9.1% |

655 trades, 50.2% win rate, average +0.07R, profit factor 1.08, about 35% of
the account invested on average. Longs made +$8,890; shorts lost $3,388 over
100 trades; crypto lost $1,113 over 21 trades.

Read this as: a thin edge on the long side in one 18-month bull market, not
proof of anything. The first version (trend-following weights) lost 1%; this is
the second configuration tried, so some of the improvement is selection.
Not modelled: short borrow fees, partial fills, halts, early closes.

## Known limits

- Yahoo data is free and unofficial; crypto volume there is mostly zero, so the
  volume signal stays neutral for crypto until Alpaca data is used.
- Alpaca's free IEX feed sees only a slice of stock volume.
- Stops are checked every 5 minutes by the bot, not held at the broker. A gap
  or an outage can fill worse than the stop.
- Half-day sessions are not in the dry-run market clock.
- The bot only runs while the program is open and the PC is awake.

## Layout

```
config.yaml           all settings
quantbot/signals.py   the seven signals, regime, weight fitting
quantbot/timemachine.py
quantbot/strategy.py  plans, sizing, exit rules
quantbot/risk.py      the risk gate
quantbot/engine.py    the hourly cycle
quantbot/backtest.py  walk-forward backtest
quantbot/broker.py    dry-run simulator and Alpaca
quantbot/data.py      Yahoo and Alpaca bars
quantbot/web/         dashboard
docs/spec.md          design
```
