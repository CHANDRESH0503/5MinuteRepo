"""Reference prices for the model, from public sources the VPS can reach.

Chainlink (the settlement oracle):
  `GET /markets/{slug}/oracle-candles?from=&to=` on Limitless returns 1-minute
  OHLC of Chainlink BTC/USD in 18-decimal units. On 9 Nov 2026 these were spot,
  not the TWAP: the mean of the minute before each bell matched the official
  open and close TWAP to within $1-4, while the bar at the bell was $9-43 off.
  So the candles give `sigma_1m` exactly as the plan defines it, and an
  independent estimate of each settlement TWAP. They arrive about two minutes
  late, so they cannot give live spot.

Spot proxy (S and the final-minute prints):
  Live Chainlink spot needs credentials we do not have. S is the median of the
  Coinbase and Kraken BTC/USD mids. This is a documented deviation from the
  plan. Its basis to Chainlink is logged every minute, and the error of the
  proxy-rebuilt close TWAP is logged on every window, so the cost of the
  approximation is measured, not assumed.
"""

from __future__ import annotations

import json
import statistics
import threading
import time
import urllib.request
from math import log
from typing import Any

UA = {"User-Agent": "fivemin-recorder/0.2 (read-only)"}
CANDLES = "https://api.limitless.exchange/markets/{slug}/oracle-candles?from={start}&to={end}"
COINBASE = "https://api.exchange.coinbase.com/products/BTC-USD/ticker"
KRAKEN = "https://api.kraken.com/0/public/Ticker?pair=XBTUSD"
FRESH_SECONDS = 1.0
SIGMA_RETURNS = 60


def _get(url: str, timeout: float = 3.0) -> Any:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


# ---- Chainlink candles (pure parsing + helpers) ----------------------------

def parse_candles(raw: dict[str, Any]) -> dict[int, dict[str, float]]:
    """Bars keyed by their open second, prices in USD. Non-Chainlink is refused."""
    if raw.get("source") != "chainlink":
        return {}
    out = {}
    for row in raw.get("rows") or []:
        out[int(row["timestamp"])] = {k: float(row[k]) / 1e18 for k in ("open", "high", "low", "close")}
    return out


MAX_CANDLE_LAG = 300  # the newest bar may be up to five minutes old (they publish ~2 min late)


def sigma_from_candles(bars: dict[int, dict[str, float]], now: float) -> float | None:
    """Std of the latest 60 one-minute log returns of Chainlink spot closes.

    Uses the newest completed bar no older than MAX_CANDLE_LAG and the 60 bars
    before it. Any missing minute in those 61 returns None.
    """
    done = [t for t in bars if t + 60 <= now]
    if not done:
        return None
    last = max(done)
    if now - (last + 60) > MAX_CANDLE_LAG:
        return None
    closes = []
    for i in range(SIGMA_RETURNS, -1, -1):
        bar = bars.get(last - 60 * i)
        if bar is None or bar["close"] <= 0:
            return None
        closes.append(bar["close"])
    returns = [log(b / a) for a, b in zip(closes, closes[1:])]
    sd = statistics.stdev(returns)
    return sd if sd > 0 else None


def twap_estimate(bars: dict[int, dict[str, float]], bell: int) -> float | None:
    """Estimate of the 60 s TWAP at `bell` from the bar before it (mean of OHLC)."""
    bar = bars.get(bell - 60)
    if bar is None:
        return None
    return (bar["open"] + bar["high"] + bar["low"] + bar["close"]) / 4.0


def fetch_candles(slug: str, start: int, end: int) -> dict[int, dict[str, float]]:
    return parse_candles(_get(CANDLES.format(slug=slug, start=start, end=end), timeout=5.0))


# ---- Spot proxy ------------------------------------------------------------

def coinbase_mid(raw: dict[str, Any]) -> float | None:
    try:
        return (float(raw["bid"]) + float(raw["ask"])) / 2.0
    except (KeyError, TypeError, ValueError):
        return None


def kraken_mid(raw: dict[str, Any]) -> float | None:
    try:
        if raw.get("error"):
            return None
        t = next(iter(raw["result"].values()))
        return (float(t["b"][0]) + float(t["a"][0])) / 2.0
    except (KeyError, TypeError, ValueError, StopIteration):
        return None


class SpotProxy:
    """Median of fresh exchange mids, stored one price per second."""

    name = "spot proxy: median(Coinbase, Kraken) mid"

    def __init__(self) -> None:
        self.by_second: dict[int, float] = {}
        self.last: tuple[float, float] | None = None  # (price, received)
        self.sources: dict[str, float | None] = {}
        self._lock = threading.Lock()

    def record(self, mids: dict[str, float | None], received: float) -> float | None:
        good = [m for m in mids.values() if m is not None and m > 0]
        with self._lock:
            self.sources = mids
            if not good:
                return None
            price = statistics.median(good)
            self.by_second[int(received)] = price  # last sample inside each second wins
            self.last = (price, received)
            return price

    def start(self, interval: float = 0.5) -> None:
        """Sample on a background thread so every second gets a price even when
        the recorder's own loop runs long. The sources are queried in parallel."""
        def fetch(name: str, url: str, parse, out: dict) -> None:
            try:
                out[name] = parse(_get(url, timeout=0.8))
            except Exception:  # noqa: BLE001
                out[name] = None

        def loop() -> None:
            while True:
                began = time.time()
                mids: dict[str, float | None] = {}
                threads = [threading.Thread(target=fetch, args=(n, u, f, mids))
                           for n, u, f in (("coinbase", COINBASE, coinbase_mid), ("kraken", KRAKEN, kraken_mid))]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                self.record(mids, time.time())
                time.sleep(max(0.0, interval - (time.time() - began)))

        threading.Thread(target=loop, daemon=True, name="spot-proxy").start()

    def poll(self) -> float | None:
        mids: dict[str, float | None] = {}
        for name, url, parse in (("coinbase", COINBASE, coinbase_mid), ("kraken", KRAKEN, kraken_mid)):
            try:
                mids[name] = parse(_get(url, timeout=0.8))
            except Exception:  # noqa: BLE001 - a missing source is just missing
                mids[name] = None
        return self.record(mids, time.time())

    def spot(self, now: float) -> float | None:
        if self.last is None or now - self.last[1] > FRESH_SECONDS:
            return None
        return self.last[0]

    def final_minute_prices(self, window_end: int, now: float) -> list[float] | None:
        """One proxy price per completed second of [end-60, end). A hole returns None."""
        prices = []
        for second in range(window_end - 60, min(int(now), window_end)):
            price = self.by_second.get(second)
            if price is None:
                return None
            prices.append(price)
        return prices

    def rebuilt_twap(self, window_end: int) -> float | None:
        prices = self.final_minute_prices(window_end, window_end)
        return None if not prices or len(prices) < 60 else sum(prices) / len(prices)

    def prune(self, now: float, keep: int = 900) -> None:
        with self._lock:
            for s in [s for s in self.by_second if s < now - keep]:
                del self.by_second[s]


def bps(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return (a / b - 1.0) * 1e4

