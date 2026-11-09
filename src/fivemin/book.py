"""Order book as Limitless publishes it. No I/O.

`GET /markets/{slug}/orderbook` returns one book, quoted in the YES (Up) token.
`size` is in raw 6-decimal units (1 share = 1,000,000); `price` is a plain
decimal. The docs say bids come highest first and asks lowest first; this
module sorts by price anyway and never trusts the wire order.

Down has no separate book: buying Down at q is selling Up at 1 - q, so the
Down book is the mirror of the Up book (`mirror`).
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


SIZE_SCALE = 1_000_000


def _levels(raw: list[dict[str, Any]] | None, scale: float) -> list[Level]:
    out = []
    for row in raw or []:
        price, size = float(row["price"]), float(row["size"]) / scale
        if size > 0:
            out.append((price, size))
    return out


def parse_book(raw: dict[str, Any], *, received: float | None = None, scale: float = SIZE_SCALE) -> Book:
    bids = sorted(_levels(raw.get("bids"), scale), key=lambda lv: lv[0], reverse=True)
    asks = sorted(_levels(raw.get("asks"), scale), key=lambda lv: lv[0])
    return Book(bids=bids, asks=asks, timestamp=None, received=received)


def mirror(book: Book) -> Book:
    """The other outcome's book: its bids are 1 - our asks, its asks 1 - our bids."""
    return Book(
        bids=[(round(1.0 - p, 6), s) for p, s in book.asks],
        asks=[(round(1.0 - p, 6), s) for p, s in book.bids],
        timestamp=book.timestamp,
        received=book.received,
    )
