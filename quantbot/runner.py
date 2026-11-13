"""Scheduler: one analysis cycle per hour, stop/target checks in between."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

from .market_clock import now_utc


def next_cycle_time(now: datetime, minute: int) -> datetime:
    t = now.replace(minute=minute, second=0, microsecond=0)
    return t if t > now else t + timedelta(hours=1)


class Runner(threading.Thread):
    def __init__(self, engine, cfg: dict):
        super().__init__(daemon=True, name="quantbot-runner")
        self.engine = engine
        # Run just after hourly bars complete: stock bars start at :30 on Yahoo, :00 on Alpaca.
        self.minute = (engine.data.stock_bar_minute + 2) % 60
        self.exit_every = timedelta(minutes=cfg["schedule"]["exit_check_minutes"])
        self.stop_event = threading.Event()
        self.next_run: datetime | None = None
        self.busy = False

    def trigger(self) -> bool:
        """Run a cycle now on a separate thread (the dashboard's Run-now button)."""
        if self.busy:
            return False
        threading.Thread(target=self._cycle, daemon=True).start()
        return True

    def _cycle(self) -> None:
        if self.busy:
            return
        self.busy = True
        try:
            out = self.engine.run_cycle()
            print(f"[{out['ts'][:19]}] cycle {out['cycle_id']}: equity ${out['equity']:,.2f} {out['summary']['counts']}")
        except Exception as exc:
            print(f"cycle failed: {exc}")
        finally:
            self.busy = False

    def run(self) -> None:
        now = now_utc()
        last = self.engine.store.latest_cycle()
        if not last or now - datetime.fromisoformat(last["ts"]) > timedelta(minutes=55):
            self._cycle()                                  # fresh picture on start-up
        self.next_run = next_cycle_time(now_utc(), self.minute)
        next_exit = now_utc() + self.exit_every
        while not self.stop_event.wait(1.0):
            now = now_utc()
            if now >= self.next_run:
                self._cycle()
                self.next_run = next_cycle_time(now_utc(), self.minute)
                next_exit = now_utc() + self.exit_every
            elif now >= next_exit:
                try:
                    for c in self.engine.check_exits():
                        print(f"[{now.isoformat()[:19]}] exit {c['symbol']}: {c['reason']} ({c['pnl']:+.2f})")
                except Exception as exc:
                    print(f"exit check failed: {exc}")
                next_exit = now_utc() + self.exit_every
