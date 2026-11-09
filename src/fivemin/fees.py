"""Taker fee curve on Limitless new trading venues.

Docs (Migrate to the new trading venues, read 9 Nov 2026): the fee is
rate_bps * N * (S - N) / (10000 * S), N the USDC paid or received before the
fee and S the shares, rounded half-up to 0.00001 USDC per match. With
N = price * S that is shares * rate * price * (1 - price), rate = bps / 10000:

fee = shares * fee_rate * price * (1 - price)        (fee_rate 0.05 at 500 bps)

Makers pay nothing. Same for buys and sells.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def fee_per_share(price: float, fee_rate: float) -> float:
    """USDC fee for one share bought or sold as a taker at `price`."""
    if price < 0.0 or price > 1.0:
        raise ValueError(f"price must be in [0, 1], got {price}")
    if fee_rate < 0.0:
        raise ValueError(f"fee_rate must be >= 0, got {fee_rate}")
    return fee_rate * price * (1.0 - price)


def fee_usdc(shares: float, price: float, fee_rate: float) -> float:
    if shares < 0.0:
        raise ValueError(f"shares must be >= 0, got {shares}")
    return shares * fee_per_share(price, fee_rate)


def taker_breakeven(price: float, fee_rate: float) -> float:
    """Win rate needed to break even buying at `price` and holding to $1.

    Cost per share is the price plus the taker fee. The winning share pays 1.
    """
    return price + fee_per_share(price, fee_rate)


def buy_edge(probability: float, ask: float, fee_rate: float) -> float:
    """Model probability minus the all-in taker cost of one share."""
    return probability - ask - fee_per_share(ask, fee_rate)


def pair_cost(ask_up: float, ask_down: float, fee_rate: float) -> float:
    """All-in cost of buying one Up and one Down as a taker."""
    return (
        ask_up
        + fee_per_share(ask_up, fee_rate)
        + ask_down
        + fee_per_share(ask_down, fee_rate)
    )


def limitless_match_fee(usdc: float, shares: float, rate_bps: int) -> float:
    """Exact per-match fee: rate_bps * N * (S - N) / (10000 * S), half-up to 1e-5."""
    if shares <= 0:
        return 0.0
    n, s = Decimal(str(usdc)), Decimal(str(shares))
    fee = Decimal(rate_bps) * n * (s - n) / (Decimal(10_000) * s)
    return float(fee.quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP))
