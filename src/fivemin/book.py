"""Order book as the CLOB publishes it. No I/O.

The raw `/book` payload and the websocket `book` event list bids ascending and
asks descending, so the best price is the *last* element on both sides (seen
on btc-updown-5m-1794211200, 9 Nov 2026). Never index `[0]` on the raw lists:
this module sorts by price and does not trust the wire order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

Level = tuple[float, float]  # (price, size)


@dataclass
class Book:
    bids: list[Level] = field(default_factory=list)  # best (highest) first
    asks: list[Level] = field(default_factory=list)  # best (lowest) first
    timestamp: float | None = None  # venue timestamp, seconds
    received: float | None = None  # our receipt time, seconds

    @property
    def best_bid(self) -> Level | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> Level | None:
        return self.asks[0] if self.asks else None

    def mid(self) -> float | None:
        if not self.bids or not self.asks:
            return None
        return (self.bids[0][0] + self.asks[0][0]) / 2.0

    def age(self, now: float) -> float:
        """Seconds since receipt. A book never received is infinitely old."""
        if self.received is None:
            return float("inf")
        return max(now - self.received, 0.0)

    def size_at(self, side: str, price: float) -> float:
        levels = self.bids if side == "bid" else self.asks
        for level_price, size in levels:
            if abs(level_price - price) < 1e-9:
                return size
        return 0.0

    def set_level(self, side: str, price: float, size: float) -> None:
        """Apply a websocket price_change: size is the new total, 0 removes."""
        levels = self.bids if side == "bid" else self.asks
        levels[:] = [(p, s) for p, s in levels if abs(p - price) >= 1e-9]
        if size > 0:
            levels.append((price, size))
        levels.sort(key=lambda lv: lv[0], reverse=(side == "bid"))


def _levels(raw: list[dict[str, Any]] | None) -> list[Level]:
    out = []
    for row in raw or []:
        price, size = float(row["price"]), float(row["size"])
        if size > 0:
            out.append((price, size))
    return out


def parse_book(raw: dict[str, Any], *, received: float | None = None) -> Book:
    bids = sorted(_levels(raw.get("bids")), key=lambda lv: lv[0], reverse=True)
    asks = sorted(_levels(raw.get("asks")), key=lambda lv: lv[0])
    ts = raw.get("timestamp")
    timestamp = None if ts in (None, "") else float(ts) / 1000.0
    return Book(bids=bids, asks=asks, timestamp=timestamp, received=received)
