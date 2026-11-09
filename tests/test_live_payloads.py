"""Raw Limitless payloads captured on 9 Nov 2026.

These check the parser against what the venue actually sends. They are not
values to substitute when a field is missing.
"""

import json
from dataclasses import replace
from pathlib import Path

from fivemin.book import mirror, parse_book
from fivemin.fees import fee_usdc, limitless_match_fee
from fivemin.market import local_call, official_outcome, parse_market, skip_reasons
from fivemin.reference import parse_candles, sigma_from_candles, twap_estimate

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name):
    return json.loads((FIXTURES / name).read_text())


def test_open_window_parses_and_can_be_priced():
    w = parse_market(_load("limitless_open.json"))
    assert w.slug.startswith("btc-up-or-down-5-min-")
    assert w.end_unix - w.open_unix == 300
    assert w.fee_rate == 0.05 and w.rebate_rate == 0.3
    assert w.lookback_seconds == 60 and w.stream_type == "twap"
    assert w.reward_min_shares == 50
    assert skip_reasons(w) == []
    assert official_outcome(w) is None


def test_resolved_window_yes_is_up():
    w = parse_market(_load("limitless_resolved.json"))
    assert w.slug == "btc-up-or-down-5-min-1794214200"
    assert w.resolution_outcome == "UP" and w.winning_index == 0
    assert official_outcome(w) == "up"
    assert abs(w.price_to_beat - 74263.908125161839) < 1e-6
    assert abs(w.close_twap - 74422.150291161597) < 1e-6
    assert local_call(w.close_twap, w.price_to_beat) == official_outcome(w)


def test_missing_or_foreign_fields_skip():
    w = parse_market(_load("limitless_open.json"))
    assert skip_reasons(replace(w, lookback_seconds=None))
    assert skip_reasons(replace(w, stream_type="spot"))
    assert skip_reasons(replace(w, fee_rate=None))
    assert skip_reasons(replace(w, slug="eth-up-or-down-5-min-1794214800"))
    assert skip_reasons(replace(w, slug=f"btc-up-or-down-5-min-{w.open_unix + 300}"))


def test_book_sizes_are_six_decimal_and_down_is_the_mirror():
    raw = _load("limitless_book.json")
    up = parse_book(raw, received=0.0)
    assert up.best_bid[0] == max(r["price"] for r in raw["bids"])
    assert up.best_bid[1] == next(r["size"] for r in raw["bids"] if r["price"] == up.best_bid[0]) / 1e6
    down = mirror(up)
    assert abs(down.best_ask[0] - (1 - up.best_bid[0])) < 1e-9
    # Captured with an empty ask side. The venue still printed midpoint 0.7755;
    # a one-sided book has no mid here, and Down has no bid.
    assert raw["asks"] == [] and raw["midpoint"] == 0.7755
    assert up.mid() is None and down.best_bid is None


def test_fee_matches_the_docs_example():
    # Docs: buying 100 shares at $0.40 costs $40.00 plus a $1.20 fee.
    assert limitless_match_fee(40.0, 100, 500) == 1.2
    assert abs(fee_usdc(100, 0.40, 0.05) - 1.2) < 1e-12
    assert limitless_match_fee(0.123456, 1, 500) == 0.00541  # half-up to 1e-5


def test_candles_are_chainlink_spot_and_rebuild_the_settlement_twap():
    bars = parse_candles(_load("limitless_candles.json"))
    w = parse_market(_load("limitless_resolved.json"))
    # The mean of the minute before each bell is within 1 bp of the official TWAP.
    assert abs(twap_estimate(bars, w.open_unix) / w.price_to_beat - 1) < 1e-4
    assert abs(twap_estimate(bars, w.end_unix) / w.close_twap - 1) < 1e-4
    sigma = sigma_from_candles(bars, now=w.open_unix + 30)
    assert sigma is not None and 1e-5 < sigma < 1e-2
    assert parse_candles({**_load("limitless_candles.json"), "source": "pyth"}) == {}


def test_price_to_beat_placeholder_before_the_open_is_none():
    m = _load("limitless_open.json")
    m["metadata"]["openPrice"] = "---"  # what the venue sends before the bell
    m["status"] = "CREATED"
    w = parse_market(m)
    assert w.price_to_beat is None and official_outcome(w) is None
