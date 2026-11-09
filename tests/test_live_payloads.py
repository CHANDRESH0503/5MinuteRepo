"""Raw payloads captured from the live venue on 9 Nov 2026.

These are the check that the parser matches what Polymarket actually sends,
not values to substitute when a field is missing.
"""

import json
from dataclasses import replace
from pathlib import Path

from fivemin.book import parse_book
from fivemin.market import local_call, official_outcome, parse_event, skip_reasons

FIXTURES = Path(__file__).parent / "fixtures"


def _event(name):
    return json.loads((FIXTURES / name).read_text())[0]


def test_open_window_parses_and_is_tradeable():
    w = parse_event(_event("gamma_open.json"))
    assert w.slug == "btc-updown-5m-1794211200"
    assert w.open_unix == 1794211200
    assert w.end_unix == 1794211200 + 300
    assert w.fee_rate == 0.07 and w.fee_exponent == 1 and w.taker_only is True
    assert w.rebate_rate == 0.2 and w.min_size == 5 and w.tick == 0.01
    assert w.lookback_seconds == 60 and w.version == "v1" and w.neg_risk is False
    assert w.up_token.startswith("6472666413") and w.down_token.startswith("2637287976")
    assert skip_reasons(w, clip=5) == []
    assert official_outcome(w) is None


def test_resolved_window_reads_the_official_outcome():
    w = parse_event(_event("gamma_resolved.json"))
    assert w.slug == "btc-updown-5m-1794210000"
    assert official_outcome(w) == "down"


def test_clip_below_min_size_is_skipped():
    w = parse_event(_event("gamma_open.json"))
    assert any("orderMinSize" in r for r in skip_reasons(w, clip=4))


def test_missing_fields_are_not_filled_with_the_nov_9_values():
    event = _event("gamma_open.json")
    m = event["markets"][0]
    for key in ("orderMinSize", "orderPriceMinTickSize"):
        m.pop(key)
    m["feeSchedule"].pop("takerOnly")
    m["feeSchedule"].pop("exponent")
    w = parse_event(event)
    assert w.min_size is None and w.tick is None and w.taker_only is None and w.fee_exponent is None
    reasons = " | ".join(skip_reasons(w, clip=5))
    for word in ("orderMinSize", "tick", "takerOnly", "exponent"):
        assert word in reasons


def test_foreign_markets_are_refused():
    w = parse_event(_event("gamma_open.json"))
    assert skip_reasons(replace(w, slug="eth-updown-5m-1794211200"), clip=5)
    assert skip_reasons(replace(w, duration="15m"), clip=5)
    assert skip_reasons(replace(w, asset="eth"), clip=5)
    assert skip_reasons(replace(w, fee_exponent=2.0), clip=5)
    assert skip_reasons(replace(w, lookback_seconds=None), clip=5)
    assert skip_reasons(replace(w, slug="btc-updown-5m-1794211500"), clip=5)  # slug != eventStartTime


def test_raw_book_best_price_is_the_last_element_on_the_wire():
    raw = json.loads((FIXTURES / "clob_book_up.json").read_text())
    assert float(raw["bids"][0]["price"]) == 0.01  # worst bid first on the wire
    book = parse_book(raw, received=0.0)
    assert book.best_bid[0] == max(float(r["price"]) for r in raw["bids"])
    assert book.best_ask[0] == min(float(r["price"]) for r in raw["asks"])
    assert book.best_bid[0] < book.best_ask[0]
    assert book.timestamp == int(raw["timestamp"]) / 1000


def test_a_tie_resolves_up():
    assert local_call(100.0, 100.0) == "up"
    assert local_call(99.99, 100.0) == "down"
