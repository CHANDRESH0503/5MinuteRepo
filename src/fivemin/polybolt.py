"""Chainlink BTC/USD from PolyBolt (wss://ws-live-v2.polymarket.com/ws).

Protocol per docs.polymarket.com/api-reference/wss/polybolt.md (read 9 Nov 2026):

  * Reference-price channels require CLOB API credentials: {"op":"auth"} with
    apiKey, secret, passphrase. They are read from the environment variables
    POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE and never logged.
  * Envelope {"v":1,"channel","seq","ts","snapshot"?,"dropped"?,"payload"}.
    A snapshot payload is a batch {symbol, source, data:[points]} covering
    the last two minutes; a live payload is one point.
  * Every point carries `source` in {pyth, chainlink, binance}. The provider pin
    may be ignored by the server, and a symbol can flip vendor mid-stream. A
    point that is not Chainlink is rejected here, not priced.
  * `full_accuracy_value` is the exact decimal string; `value` is a float.

`ChainlinkState` is pure and tested. `PolyBoltFeed` runs the socket in a
thread and implements recorder.ReferenceFeed.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import threading
import time
from pathlib import Path
from typing import Any

URL = "wss://ws-live-v2.polymarket.com/ws"
SYMBOL = "btcusd"
FRESH_SECONDS = 1.0
CREDENTIAL_ENV = ("POLY_API_KEY", "POLY_API_SECRET", "POLY_API_PASSPHRASE")


def credentials_from_env() -> dict[str, str] | None:
    key, secret, passphrase = (os.environ.get(name) for name in CREDENTIAL_ENV)
    if not (key and secret and passphrase):
        return None
    return {"apiKey": key, "secret": secret, "passphrase": passphrase}


def subscribe_frame() -> dict[str, Any]:
    return {
        "op": "subscribe",
        "rid": "s1",
        "subscriptions": [
            {"channel": "price.crypto", "filter": {"symbol": SYMBOL, "provider": "chainlink"}},
            {"channel": "price.crypto.twap", "filter": {"symbol": SYMBOL, "window_seconds": 60, "provider": "chainlink"}},
        ],
    }


class ChainlinkState:
    """Per-second Chainlink spot and 60 s TWAP prints, keyed by source time."""

    def __init__(self) -> None:
        self.spot_by_second: dict[int, float] = {}
        self.twap_by_second: dict[int, float] = {}
        self.last_spot: tuple[float, float] | None = None  # (price, received)
        self.rejected: dict[str, int] = {}
        self.holes: list[tuple[str, int]] = []  # (channel, at seq) where frames were lost
        self._seq: dict[str, int] = {}

    def _reject(self, why: str) -> None:
        self.rejected[why] = self.rejected.get(why, 0) + 1

    def _point(self, channel: str, point: dict, source: str | None, received: float) -> None:
        if source != "chainlink":
            self._reject(f"source {source}")
            return
        if point.get("symbol", SYMBOL).lower() != SYMBOL:
            self._reject("symbol")
            return
        try:
            price = float(point["full_accuracy_value"])
            second = int(point["timestamp"]) // 1000
        except (KeyError, TypeError, ValueError):
            self._reject("malformed point")
            return
        if not price > 0:
            self._reject("non-positive price")
            return
        if channel == "price.crypto":
            self.spot_by_second[second] = price
            if self.last_spot is None or second >= max(self.spot_by_second):
                self.last_spot = (price, received)
        else:
            self.twap_by_second[second] = price

    def on_envelope(self, env: dict, received: float) -> None:
        channel = env.get("channel")
        if channel not in ("price.crypto", "price.crypto.twap"):
            return
        seq = env.get("seq")
        if env.get("dropped"):
            self.holes.append((channel, seq if isinstance(seq, int) else -1))
        if isinstance(seq, int):
            last = self._seq.get(channel)
            if last is not None and seq != last + 1 and not env.get("snapshot"):
                self.holes.append((channel, seq))
            self._seq[channel] = seq
        payload = env.get("payload") or {}
        if channel == "price.crypto.twap":
            window = payload.get("window_seconds")
            if window != 60:
                self._reject(f"twap window {window}")
                return
        if "data" in payload:  # snapshot batch: source once at batch level
            for point in payload.get("data") or []:
                self._point(channel, {**point, "symbol": payload.get("symbol", SYMBOL)}, payload.get("source"), received)
        else:
            self._point(channel, payload, payload.get("source"), received)

    def reset_sequence(self) -> None:
        """After a reconnect seq restarts; compare only within one connection."""
        self._seq.clear()

    # recorder.ReferenceFeed -------------------------------------------------
    def spot(self, now: float | None = None) -> float | None:
        if self.last_spot is None:
            return None
        price, received = self.last_spot
        if (now if now is not None else time.time()) - received > FRESH_SECONDS:
            return None  # stale: no last-known-price trade
        return price

    def twap60_at(self, unix: int) -> float | None:
        """The TWAP print on the bell: source time inside second `unix`, or None."""
        return self.twap_by_second.get(int(unix))

    def minute_spots(self, now: float | None = None, minutes: int = 61) -> list[float]:
        """Spot at consecutive completed minute marks, oldest first, stopping at a hole."""
        now = now if now is not None else time.time()
        last_minute = int(now) // 60 * 60
        out: list[float] = []
        for m in range(minutes):
            price = self.spot_by_second.get(last_minute - 60 * m)
            if price is None:
                break
            out.append(price)
        return out[::-1]

    def final_minute_prices(self, window_end: int, now: float) -> list[float] | None:
        """One spot per completed second of [end-60, end). Any hole returns None."""
        start = window_end - 60
        upto = min(int(now), window_end)
        prices = []
        for second in range(start, upto):
            price = self.spot_by_second.get(second)
            if price is None:
                return None
            prices.append(price)
        return prices

    def prune(self, now: float, keep_seconds: int = 4000) -> None:
        cutoff = int(now) - keep_seconds
        for book in (self.spot_by_second, self.twap_by_second):
            for second in [s for s in book if s < cutoff]:
                del book[second]


class PolyBoltFeed:
    """Background socket feeding a ChainlinkState. Raw frames go to `raw_log`."""

    name = "chainlink via polybolt"

    def __init__(self, credentials: dict[str, str], raw_log: Path | None = None) -> None:
        self._credentials = credentials
        self.state = ChainlinkState()
        self.raw_log = raw_log
        self.status = "connecting"
        self._lock = threading.Lock()
        threading.Thread(target=lambda: asyncio.run(self._run()), daemon=True).start()

    def _raw(self, text: str) -> None:
        if self.raw_log is not None:
            self.raw_log.parent.mkdir(parents=True, exist_ok=True)
            with self.raw_log.open("a") as fh:
                fh.write(json.dumps({"rx": round(time.time(), 3), "frame": text}) + "\n")

    async def _run(self) -> None:
        import websockets  # optional dependency: pip install 'fivemin[live]'

        while True:
            try:
                async with websockets.connect(URL, max_size=1 << 16) as ws:
                    with self._lock:
                        self.state.reset_sequence()
                    await ws.send(json.dumps({"op": "auth", "rid": "a1", "auth": self._credentials}))
                    await ws.send(json.dumps(subscribe_frame()))
                    self.status = "subscribed"
                    async for text in ws:
                        received = time.time()
                        self._raw(text)
                        msg = json.loads(text)
                        if msg.get("op") == "error":
                            self.status = f"error: {msg.get('code') or msg.get('message')}"
                            continue
                        with self._lock:
                            self.state.on_envelope(msg, received)
                            self.state.prune(received)
            except Exception as exc:  # noqa: BLE001
                code = getattr(exc, "code", None) or getattr(getattr(exc, "rcvd", None), "code", None)
                self.status = f"closed {code or type(exc).__name__}"
                if code in (4001, 4008):  # bad credentials or a client bug: do not hammer
                    await asyncio.sleep(60)
                else:
                    await asyncio.sleep(random.uniform(0, 10))

    def spot(self) -> float | None:
        with self._lock:
            return self.state.spot()

    def twap60_at(self, unix: int) -> float | None:
        with self._lock:
            return self.state.twap60_at(unix)

    def minute_spots(self) -> list[float]:
        with self._lock:
            return self.state.minute_spots()

    def final_minute_prices(self, window_end: int, now: float) -> list[float] | None:
        with self._lock:
            return self.state.final_minute_prices(window_end, now)
