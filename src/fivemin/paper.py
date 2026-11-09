"""Paper fill model from IMPLEMENTATION.md. No I/O, no orders.

Fills replicate the displayed book. No fill at the mid, no fill through size
that is not displayed, no fill from a quote update that has no trade. The
queue model is an approximation: a cancel of displayed size never moves us up,
because the public book does not say whether it was ahead of us.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fivemin.book import Book, Level
from fivemin.fees import fee_per_share, fee_usdc
from fivemin.strategy import FRESH_BOOK_SECONDS

UNDER_10C = 0.10


@dataclass(frozen=True)
class TakeFill:
    lots: tuple[Level, ...]
    shares: float
    price: float  # size-weighted price of the lots walked
    fee: float  # sum of per-lot fees, not the fee of the average price

    @property
    def cost(self) -> float:
        return sum(p * s for p, s in self.lots) + self.fee


def walk_asks(asks: list[Level], *, limit: float, clip: float, fee_rate: float) -> TakeFill | None:
    """Buy `clip` shares walking displayed asks at or better than `limit`.

    Returns None when displayed size through the limit is short of the clip.
    There is no partial clip.
    """
    if clip <= 0:
        return None
    lots: list[Level] = []
    need = clip
    for price, size in sorted(asks, key=lambda lv: lv[0]):
        if price > limit + 1e-9 or need <= 1e-12:
            break
        take = min(size, need)
        lots.append((price, take))
        need -= take
    if need > 1e-9:
        return None
    shares = sum(s for _, s in lots)
    price = sum(p * s for p, s in lots) / shares
    fee = sum(fee_usdc(s, p, fee_rate) for p, s in lots)
    return TakeFill(lots=tuple(lots), shares=shares, price=price, fee=fee)


@dataclass
class RestingOrder:
    """A post-only paper quote. side is 'bid' (buy) or 'ask' (sell)."""

    side: str
    price: float
    size: float
    size_ahead: float
    filled: float = 0.0
    fills: list[Level] = field(default_factory=list)

    @property
    def remaining(self) -> float:
        return self.size - self.filled

    def on_trade(self, price: float, size: float) -> float:
        """Apply a public trade print. Returns shares filled to us, at our price."""
        if self.remaining <= 1e-12 or size <= 0:
            return 0.0
        at_or_through = price <= self.price + 1e-9 if self.side == "bid" else price >= self.price - 1e-9
        if not at_or_through:
            return 0.0
        cleared = min(self.size_ahead, size)
        self.size_ahead -= cleared
        left = size - cleared
        got = min(left, self.remaining)
        if got > 0:
            self.filled += got
            self.fills.append((self.price, got))
        return got


def rest(side: str, price: float, size: float, book: Book) -> RestingOrder | None:
    """Join the queue behind displayed size. None if the quote would cross.

    A bid at or above the best ask, or an ask at or below the best bid, is a
    take, not a passive quote, and is not rested.
    """
    if side == "bid":
        if book.best_ask is not None and price >= book.best_ask[0] - 1e-9:
            return None
    elif side == "ask":
        if book.best_bid is not None and price <= book.best_bid[0] + 1e-9:
            return None
    else:
        raise ValueError(f"side must be 'bid' or 'ask', got {side!r}")
    return RestingOrder(side=side, price=price, size=size, size_ahead=book.size_at(side, price))


def one_second_markout(direction: str, fill_price: float, book: Book, now: float) -> float | None:
    """Mid one second after the fill minus our price (buy), or the reverse (sell).

    None when either touch is missing or the book is older than one second;
    the caller counts it in the missing column and leaves it out of the mean.
    """
    mid = book.mid()
    if mid is None or book.age(now) > FRESH_BOOK_SECONDS:
        return None
    return mid - fill_price if direction == "buy" else fill_price - mid


def settlement_markout(fill_price: float, won: bool, *, taker_fee_per_share: float = 0.0) -> float:
    """Per-share result of a buy held to redeem. Makers pass a zero fee."""
    return (1.0 if won else 0.0) - fill_price - taker_fee_per_share


def rebate_estimate(rebate_rate: float, fill_price: float, fee_rate: float, shares: float) -> float:
    """Separate line. Never added to the judged result."""
    return rebate_rate * fee_per_share(fill_price, fee_rate) * shares


def sleeve(price: float) -> str | None:
    return "under_10c" if price < UNDER_10C - 1e-9 else None
