"""Residual decisions. No I/O, no orders.

A take is allowed only when model probability minus the ask minus the taker
fee clears the residual. A passive quote is allowed only the same residual
through the model. A split is allowed only when the two passive bids sum to
0.99 or less, so a double fill locks a cent. Selling is not a decision here:
inventory is merged or redeemed.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil, floor

from fivemin.fees import buy_edge, pair_cost

TICK = 0.01
MIN_PRICE = 0.01
MAX_PRICE = 0.99
SPLIT_MAX_SUM = 0.99
FRESH_BOOK_SECONDS = 1.0


def _floor_tick(price: float, tick: float = TICK) -> float:
    return floor(price / tick + 1e-9) * tick


def _ceil_tick(price: float, tick: float = TICK) -> float:
    return ceil(price / tick - 1e-9) * tick


def _on_book(price: float) -> float | None:
    if price < MIN_PRICE or price > MAX_PRICE:
        return None
    return round(price, 2)


@dataclass(frozen=True)
class Decision:
    take_side: str | None
    take_limit: float | None
    take_edge: float | None
    bid_up: float | None
    ask_up: float | None
    bid_down: float | None
    ask_down: float | None
    split_ok: bool
    pair_lock: bool
    reason: str


def _quotes(probability: float, edge: float) -> tuple[float | None, float | None]:
    bid = _on_book(_floor_tick(probability - edge))
    ask = _on_book(_ceil_tick(probability + edge))
    return bid, ask


def decide(
    p_up: float | None,
    *,
    ask_up: float | None,
    ask_up_size: float,
    ask_down: float | None,
    ask_down_size: float,
    fee_rate: float,
    min_edge: float,
    clip: float,
    up_book_age_seconds: float,
    down_book_age_seconds: float,
) -> Decision:
    if p_up is None or not 0.0 <= p_up <= 1.0:
        return Decision(None, None, None, None, None, None, None, False, False, "no fair value")

    p_down = 1.0 - p_up
    bid_up, ask_up_quote = _quotes(p_up, min_edge)
    bid_down, ask_down_quote = _quotes(p_down, min_edge)
    split_ok = (
        bid_up is not None
        and bid_down is not None
        and (bid_up + bid_down) <= SPLIT_MAX_SUM + 1e-9
    )

    pair_lock = False
    if ask_up is not None and ask_down is not None:
        pair_lock = pair_cost(ask_up, ask_down, fee_rate) < 1.0

    candidates: list[tuple[float, str, float]] = []
    if (
        ask_up is not None
        and ask_up_size >= clip
        and up_book_age_seconds <= FRESH_BOOK_SECONDS
    ):
        edge = buy_edge(p_up, ask_up, fee_rate)
        if edge >= min_edge:
            candidates.append((edge, "up", ask_up))
    if (
        ask_down is not None
        and ask_down_size >= clip
        and down_book_age_seconds <= FRESH_BOOK_SECONDS
    ):
        edge = buy_edge(p_down, ask_down, fee_rate)
        if edge >= min_edge:
            candidates.append((edge, "down", ask_down))

    take_side = take_limit = take_edge = None
    reason = "quote" if any(q is not None for q in (bid_up, ask_up_quote, bid_down, ask_down_quote)) else "skip"
    if candidates:
        take_edge, take_side, take_limit = max(candidates, key=lambda row: row[0])
        reason = f"take {take_side}"
    elif pair_lock:
        reason = "pair under a dollar, log only"

    return Decision(
        take_side=take_side,
        take_limit=take_limit,
        take_edge=take_edge,
        bid_up=bid_up,
        ask_up=ask_up_quote,
        bid_down=bid_down,
        ask_down=ask_down_quote,
        split_ok=split_ok,
        pair_lock=pair_lock,
        reason=reason,
    )
