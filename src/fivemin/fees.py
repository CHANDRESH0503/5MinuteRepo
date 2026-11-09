"""Taker fee curve from Polymarket's crypto schedule.

fee = shares * fee_rate * price * (1 - price)

Makers pay nothing. The rate is read from each market's feeSchedule.
The live BTC 5-minute markets checked on 9 Nov 2026 published rate 0.07.
"""

from __future__ import annotations


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
