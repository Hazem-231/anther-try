"""Hourly bar providers. Both return a DataFrame with open/high/low/close/volume
on a tz-aware UTC index holding only *completed* regular-session bars."""
from __future__ import annotations

import time
from datetime import datetime, timedelta

import pandas as pd
import requests

from .config import CACHE_DIR
from .market_clock import NY, UTC, now_utc

COLUMNS = ["open", "high", "low", "close", "volume"]


class DataError(RuntimeError):
    pass


def bar_end(start: datetime, asset_class: str) -> datetime:
    end = start + timedelta(hours=1)
    if asset_class == "stocks":
        close = start.astimezone(NY).replace(hour=16, minute=0, second=0, microsecond=0)
        end = min(end, close.astimezone(UTC))
    return end


def drop_incomplete(df: pd.DataFrame, asset_class: str, now: datetime) -> pd.DataFrame:
    """Remove every trailing bar that has not finished yet."""
    while len(df) and now < bar_end(df.index[-1].to_pydatetime(), asset_class):
        df = df.iloc[:-1]
    return df


def on_grid(df: pd.DataFrame, asset_class: str, stock_minute: int) -> pd.DataFrame:
    """Keep only rows that start on the hourly grid inside the session. Yahoo
    adds a row stamped "right now" after the forming bar, and a closing-print
    row on half days; neither is an hourly bar."""
    idx = df.index
    keep = (idx.second == 0) & (idx.microsecond == 0)
    if asset_class == "stocks":
        ny = idx.tz_convert("America/New_York")
        minutes = ny.hour * 60 + ny.minute
        keep &= (ny.minute == stock_minute) & (minutes >= 9 * 60) & (minutes < 16 * 60)
    else:
        keep &= idx.minute == 0
    return df[keep]


class YahooData:
    """Yahoo's unofficial chart endpoint: free, no key, can lag or change."""

    name = "yahoo"
    stock_bar_minute = 30          # hourly stock bars start at :30
    hosts = ("query2.finance.yahoo.com", "query1.finance.yahoo.com")

    def __init__(self, pause: float = 0.25):
        self.pause = pause
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        self.last_price: dict[str, float] = {}

    @staticmethod
    def ticker(symbol: str) -> str:
        return symbol.replace("/", "-")

    def _chart(self, symbol: str, params: dict) -> dict:
        last_err = None
        for attempt in range(3):
            for host in self.hosts:
                url = f"https://{host}/v8/finance/chart/{self.ticker(symbol)}"
                try:
                    resp = self.session.get(url, params=params, timeout=20)
                    if resp.status_code == 200:
                        result = resp.json()["chart"]["result"]
                        if result:
                            return result[0]
                        last_err = "empty result"
                    else:
                        last_err = f"HTTP {resp.status_code}"
                except (requests.RequestException, ValueError, KeyError) as exc:
                    last_err = str(exc)
            time.sleep(2 + 3 * attempt)
        raise DataError(f"Yahoo chart failed for {symbol}: {last_err}")

    def bars(self, symbol: str, asset_class: str, days: int, now: datetime | None = None) -> pd.DataFrame:
        now = now or now_utc()
        days = min(days, 729)                       # Yahoo keeps ~730 days of 1h bars
        res = self._chart(
            symbol,
            {
                "interval": "1h",
                "period1": int((now - timedelta(days=days)).timestamp()),
                "period2": int(now.timestamp()) + 60,
                "includePrePost": "false",
            },
        )
        ts = res.get("timestamp") or []
        quote = res["indicators"]["quote"][0]
        df = pd.DataFrame({k: quote.get(k) for k in COLUMNS}, index=pd.to_datetime(ts, unit="s", utc=True))
        df = df.dropna(subset=["open", "high", "low", "close"])
        df["volume"] = df["volume"].fillna(0.0)
        df = df[~df.index.duplicated(keep="last")].sort_index().astype(float)
        price = res.get("meta", {}).get("regularMarketPrice")
        if price:
            self.last_price[symbol] = float(price)
        elif len(df):
            self.last_price[symbol] = float(df["close"].iloc[-1])
        time.sleep(self.pause)
        return drop_incomplete(on_grid(df, asset_class, self.stock_bar_minute), asset_class, now)

    def latest_price(self, symbol: str, asset_class: str) -> float:
        res = self._chart(symbol, {"interval": "5m", "range": "1d", "includePrePost": "false"})
        price = res.get("meta", {}).get("regularMarketPrice")
        if not price:
            raise DataError(f"no price for {symbol}")
        self.last_price[symbol] = float(price)
        time.sleep(self.pause)
        return float(price)


class AlpacaData:
    """Alpaca market data. Stock bars start on the hour; the 09:00 bar holds the
    first half hour of the session."""

    name = "alpaca"
    stock_bar_minute = 0

    def __init__(self, key: str, secret: str, feed: str = "iex"):
        from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient

        # raw_data=True returns plain dicts; parsing them ourselves is far
        # quicker than building one object per bar.
        self.stocks = StockHistoricalDataClient(key, secret, raw_data=True)
        self.crypto = CryptoHistoricalDataClient(key, secret, raw_data=True)
        self.feed = feed
        self.last_price: dict[str, float] = {}
        self._cache: dict[str, pd.DataFrame] = {}     # symbol -> bars, may end with a forming bar
        self._loaded_at: dict[str, datetime] = {}
        self._full_day: dict[str, object] = {}        # asset class -> New York date of the last full download
        self._disk = CACHE_DIR / f"alpaca_live_{feed}.pkl"
        try:                                          # history survives a restart
            blob = pd.read_pickle(self._disk)
            self._cache, self._full_day = blob["cache"], blob["full_day"]
        except Exception:
            pass

    def _save(self) -> None:
        try:
            CACHE_DIR.mkdir(exist_ok=True)
            pd.to_pickle({"cache": self._cache, "full_day": self._full_day}, self._disk)
        except OSError:
            pass

    def _fetch(self, symbols: list[str], asset_class: str, start: datetime, end: datetime) -> dict[str, pd.DataFrame]:
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        if asset_class == "stocks":
            raw = self.stocks.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=list(symbols), timeframe=TimeFrame.Hour, start=start, end=end,
                adjustment=Adjustment.ALL, feed=DataFeed(self.feed),
            ))
        else:
            raw = self.crypto.get_crypto_bars(CryptoBarsRequest(
                symbol_or_symbols=list(symbols), timeframe=TimeFrame.Hour, start=start, end=end))
        out = {}
        for sym, rows in (raw or {}).items():
            if not rows:
                continue
            df = pd.DataFrame(rows).rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
            df.index = pd.to_datetime(df["t"], utc=True)
            df.index.name = None
            df = df[COLUMNS].astype(float).sort_index()
            if asset_class == "stocks":
                hour = df.index.tz_convert("America/New_York").hour
                df = df[(hour >= 9) & (hour <= 15)]          # regular session only
            out[sym] = df
        return out

    def preload(self, symbols: list[str], asset_class: str, days: int, now: datetime | None = None) -> None:
        """Load a whole asset class in one request. The first call of the day
        downloads the full history (stock prices are re-adjusted for splits and
        dividends); later calls fetch only the last few days and merge."""
        now = now or now_utc()
        today = now.astimezone(NY).date()
        symbols = list(symbols)
        have_all = all(s in self._cache for s in symbols)
        full = not have_all or (asset_class == "stocks" and self._full_day.get(asset_class) != today)
        if full:
            fetched = self._fetch(symbols, asset_class, now - timedelta(days=days), now)
            self._cache.update(fetched)
            if all(s in fetched for s in symbols):
                self._full_day[asset_class] = today
        else:
            since = min(self._cache[s].index[-1] for s in symbols).to_pydatetime() - timedelta(days=3)
            fetched = self._fetch(symbols, asset_class, since, now)
            floor = pd.Timestamp(now - timedelta(days=days))
            for s, new in fetched.items():
                old = self._cache[s]
                merged = pd.concat([old[old.index < new.index[0]], new])
                self._cache[s] = merged[merged.index >= floor]
        for s in fetched:
            self._loaded_at[s] = now
        self._save()

    def bars(self, symbol: str, asset_class: str, days: int, now: datetime | None = None) -> pd.DataFrame:
        now = now or now_utc()
        if self._loaded_at.get(symbol) != now:
            self.preload([symbol], asset_class, days, now)
        if self._loaded_at.get(symbol) != now:
            raise DataError(f"Alpaca returned no bars for {symbol}")
        df = self._cache[symbol]
        self.last_price[symbol] = float(df["close"].iloc[-1])
        return drop_incomplete(df, asset_class, now)

    def latest_price(self, symbol: str, asset_class: str) -> float:
        from alpaca.data.requests import CryptoLatestTradeRequest, StockLatestTradeRequest

        if asset_class == "stocks":
            from alpaca.data.enums import DataFeed

            trade = self.stocks.get_stock_latest_trade(
                StockLatestTradeRequest(symbol_or_symbols=symbol, feed=DataFeed(self.feed))
            )[symbol]
        else:
            trade = self.crypto.get_crypto_latest_trade(
                CryptoLatestTradeRequest(symbol_or_symbols=symbol)
            )[symbol]
        price = float(trade["p"] if isinstance(trade, dict) else trade.price)
        self.last_price[symbol] = price
        return price


class CachedData:
    """Disk cache around a provider, for backtests (avoids refetching 2 years)."""

    def __init__(self, inner, max_age_hours: float = 12):
        self.inner = inner
        self.name = inner.name
        self.stock_bar_minute = inner.stock_bar_minute
        self.max_age = max_age_hours * 3600
        self.last_price = inner.last_price

    def preload(self, symbols: list[str], asset_class: str, days: int, now: datetime | None = None) -> None:
        """Batch-load whatever is not already on disk, if the provider can."""
        now = now or now_utc()
        missing = [s for s in symbols if not self._fresh(self._path(s, days))]
        if missing and hasattr(self.inner, "preload"):
            self.inner.preload(missing, asset_class, days, now)
            self._now = now

    def _path(self, symbol: str, days: int):
        return CACHE_DIR / f"{self.name}_{symbol.replace('/', '-')}_{days}d_1h.pkl"

    def _fresh(self, path) -> bool:
        return path.exists() and time.time() - path.stat().st_mtime < self.max_age

    def bars(self, symbol: str, asset_class: str, days: int, now: datetime | None = None) -> pd.DataFrame:
        now = now or getattr(self, "_now", None)
        CACHE_DIR.mkdir(exist_ok=True)
        path = CACHE_DIR / f"{self.name}_{symbol.replace('/', '-')}_{days}d_1h.pkl"
        if path.exists() and time.time() - path.stat().st_mtime < self.max_age:
            return pd.read_pickle(path)
        df = self.inner.bars(symbol, asset_class, days, now)
        df.to_pickle(path)
        return df

    def latest_price(self, symbol: str, asset_class: str) -> float:
        return self.inner.latest_price(symbol, asset_class)


def make_data(cfg: dict, mode: dict):
    source = cfg["data"].get("source", "auto")
    if source == "alpaca" or (source == "auto" and mode["key"]):
        if not mode["key"]:
            raise DataError("data.source is 'alpaca' but no API keys are set in .env")
        return AlpacaData(mode["key"], mode["secret"], cfg["data"].get("alpaca_feed", "iex"))
    return YahooData()
