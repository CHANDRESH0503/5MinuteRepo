from math import log

from fivemin.book import Book
from fivemin.fair import fair_up, sigma_1m
from fivemin.fees import fee_usdc
from fivemin.paper import (
    one_second_markout,
    rebate_estimate,
    rest,
    settlement_markout,
    sleeve,
    walk_asks,
)


def test_walk_charges_each_lot_at_its_own_price():
    fill = walk_asks([(0.56, 10), (0.55, 3)], limit=0.56, clip=5, fee_rate=0.07)
    assert fill.lots == ((0.55, 3), (0.56, 2))
    assert abs(fill.price - (0.55 * 3 + 0.56 * 2) / 5) < 1e-12
    assert abs(fill.fee - (fee_usdc(3, 0.55, 0.07) + fee_usdc(2, 0.56, 0.07))) < 1e-12


def test_walk_refuses_a_partial_clip():
    assert walk_asks([(0.55, 3), (0.57, 10)], limit=0.56, clip=5, fee_rate=0.07) is None


def test_crossing_quote_is_not_rested():
    book = Book(bids=[(0.47, 100)], asks=[(0.50, 100)], received=0)
    assert rest("bid", 0.50, 5, book) is None
    assert rest("ask", 0.47, 5, book) is None
    assert rest("bid", 0.47, 5, book).size_ahead == 100


def test_trades_clear_the_queue_before_filling_us():
    order = rest("bid", 0.47, 5, Book(bids=[(0.47, 8)], asks=[(0.53, 1)], received=0))
    assert order.on_trade(0.47, 6) == 0  # 8 ahead -> 2 ahead
    assert order.on_trade(0.48, 50) == 0  # above our bid: not us
    assert order.on_trade(0.46, 4) == 2  # through our price: clear 2, fill 2 at our limit
    assert order.fills == [(0.47, 2)]
    assert order.on_trade(0.47, 100) == 3  # never more than our remaining size
    assert order.remaining == 0


def test_markouts():
    book = Book(bids=[(0.50, 1)], asks=[(0.52, 1)], received=10.0)
    assert abs(one_second_markout("buy", 0.47, book, now=10.5) - 0.04) < 1e-12
    assert abs(one_second_markout("sell", 0.53, book, now=10.5) - 0.02) < 1e-12
    assert one_second_markout("buy", 0.47, book, now=12.0) is None  # stale
    assert one_second_markout("buy", 0.47, Book(bids=[(0.5, 1)], received=10), now=10) is None
    assert abs(settlement_markout(0.55, True, taker_fee_per_share=0.017325) - 0.432675) < 1e-12
    assert settlement_markout(0.47, False) == -0.47
    assert abs(rebate_estimate(0.2, 0.5, 0.07, 10) - 0.035) < 1e-12
    assert sleeve(0.09) == "under_10c" and sleeve(0.10) is None


def test_fair_value_guards():
    assert fair_up(float("nan"), 100, 0.001, 200) is None
    assert fair_up(100, 100, float("inf"), 200) is None
    assert fair_up(100, 100, 0.001, 200, lookback_seconds=None) is None
    # 30 seconds left needs 30 printed seconds. 20 means a hole.
    assert fair_up(100, 100, 0.001, 30, final_minute_prices=[100.0] * 20) is None
    assert fair_up(100, 100, 0.001, 30, final_minute_prices=[100.0] * 30) is not None


def test_a_tied_finished_average_is_up_and_the_floor_is_five_percent():
    assert fair_up(100, 100, 0.001, 0, final_minute_prices=[100.0] * 60) == 1.0
    assert abs(fair_up(99_000, 100_000, 0.0002, 240) - 0.05) < 1e-12


def test_sigma_needs_sixty_returns():
    assert sigma_1m([100.0] * 60) is None
    assert sigma_1m([100.0] * 61) is None  # zero deviation
    prices = [100.0 * (1.001 if i % 2 else 1.0) for i in range(61)]
    s = sigma_1m(prices)
    assert s is not None and abs(s - abs(log(1.001)) * (60 / 59) ** 0.5) < 1e-9
