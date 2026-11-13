# QuantBot — hourly quant trading bot for Alpaca

Approved 2026-11-13.

## Goal

A program that runs at home, analyses a fixed list of US stocks/ETFs and crypto
every hour, decides buy / sell / short / hold per symbol, places the orders
through Alpaca, and shows the full picture on a local website.

## Decisions

| Topic | Decision |
|---|---|
| Markets | US stocks & ETFs (market hours) and crypto (24/7) |
| Brain | Pure quant ensemble, no LLM in the loop |
| Direction | Long and short for stocks; crypto long-only (Alpaca rule) |
| Default mode | Dry-run simulator with no keys; Alpaca paper with keys |
| Live trading | Only when the user sets two values in `.env` themselves |
| Stack | Python, pandas/numpy, alpaca-py, FastAPI, SQLite |

## Hourly cycle

1. Fetch hourly bars (Alpaca with keys, Yahoo chart endpoint without).
2. Score every symbol on seven directional signals in [-1, +1]:
   trend, momentum, mean reversion, breakout, volume pressure,
   relative strength, chart setups.
3. Combine with weights into a conviction score; tilt by market regime.
4. Manage open trades: stop, target, signal reversal, time stop.
5. For flat symbols above the entry threshold: build a plan (ATR stop,
   R-multiple target), run the Time Machine, size the trade, run the risk gate.
6. Submit orders, record every decision with its reasons.

A lighter loop checks stops and targets every 5 minutes.

## Borrowed from Jakeschincariol/day-trader-skill (MIT)

Methods only, re-implemented here:

- Setups: 20-day breakout, bull flag, 20-day-average reclaim, oversold bounce,
  opening-range breakout (adapted to the first hourly bar). Bearish mirrors of
  these are our own extension.
- Time Machine: replays drawn from past bars, stop checked before target,
  fixed seed, verdict versus break-even 1/(1+R). Ours draws from bars that
  followed a similar conviction reading ("analogs") so the odds are conditional
  on the signal.
- Risk gate: PASS or BLOCK per order with a reason for every check.
- Sizing: quantity = max loss per trade / (entry - stop).

Not borrowed: per-order human approval. The skill refuses to trade from a
scheduled task; this bot is unattended by design, so paper mode is the default.

## Weights

Signal weights start from priors in `config.yaml` and are blended 50/50 with
weights proportional to each signal's positive rank correlation with forward
returns over a trailing window. If no signal has positive correlation, new
entries for that asset class are switched off until the next refit.

## Backtest

Walk-forward over about two years of hourly bars: weights are fitted only on
data before each test block; signals at a bar's close fill at the next bar's
open; stops are checked before targets; slippage and crypto fees are charged.
Reported: return, Sharpe, max drawdown, win rate, average R, profit factor,
and buy-and-hold comparisons.

## Dashboard

Local FastAPI site on 127.0.0.1: mode badge, account, equity curve, positions,
the latest decision per symbol with signal breakdown, Time Machine odds, risk
checks, next-run countdown, what would trigger a trade, orders, closed trades,
backtest results.

## Out of scope

Options, leverage beyond 1x gross, LLM/news-driven decisions, tax lots,
13F/congress tracking, broker-side bracket orders.
