"""Settings (config.yaml) and trading mode (.env)."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"
CACHE_DIR = ROOT / "data_cache"

BARS_PER_DAY = {"stocks": 7, "crypto": 24}
LIVE_PHRASE = "I-ACCEPT-THE-RISK"


class ConfigError(RuntimeError):
    pass


def load_config(path: str | Path | None = None) -> dict:
    path = Path(path) if path else ROOT / "config.yaml"
    with open(path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    total = sum(cfg["strategy"]["weights"].values())
    if abs(total - 1.0) > 1e-6:
        raise ConfigError(f"strategy.weights must sum to 1 (got {total:.3f})")
    return cfg


def resolve_mode(env: dict | None = None) -> dict:
    """Decide dry-run / paper / live from environment variables.

    Live needs keys, ALPACA_PAPER=false and the confirmation phrase. Anything
    short of that is refused rather than silently downgraded or upgraded.
    """
    if env is None:
        load_dotenv(ROOT / ".env")
        env = dict(os.environ)
    key = (env.get("ALPACA_API_KEY") or "").strip()
    secret = (env.get("ALPACA_SECRET_KEY") or "").strip()
    if not key or not secret:
        return {"mode": "dry-run", "key": None, "secret": None}
    paper = (env.get("ALPACA_PAPER") or "true").strip().lower() not in ("false", "0", "no")
    if paper:
        return {"mode": "paper", "key": key, "secret": secret}
    if (env.get("LIVE_TRADING_CONFIRM") or "").strip() != LIVE_PHRASE:
        raise ConfigError(
            "ALPACA_PAPER=false but LIVE_TRADING_CONFIRM is not set to "
            f"'{LIVE_PHRASE}'. Refusing to start. Set ALPACA_PAPER=true for paper "
            "trading, or add the phrase to .env yourself to trade real money."
        )
    return {"mode": "live", "key": key, "secret": secret}


def class_of(symbol: str, cfg: dict) -> str:
    return "crypto" if symbol in cfg["universe"]["crypto"] else "stocks"
