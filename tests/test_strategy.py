from datetime import datetime, timezone

from fivemin.market import parse_event, slug_for, window_start
from fivemin.session import min_edge
from fivemin.strategy import decide


def _decide(**overrides):
    args = dict(
        ask_up=0.55,
        ask_up_size=10,
        ask_down=0.50,
        ask_down_size=10,
        fee_rate=0.07,
        min_edge=0.03,
        clip=5,
        up_book_age_seconds=0.2,
        down_book_age_seconds=0.2,
    )
    args.update(overrides)
    return decide(args.pop("p_up", 0.70), **args)


def test_a_five_cent_residual_takes_and_holds_the_ask():
    decision = _decide(p_up=0.70, ask_up=0.55)
    assert decision.take_side == "up"
    assert decision.take_limit == 0.55
    assert decision.take_edge is not None
    assert decision.take_edge >= 0.03


def test_the_sixty_cent_worked_example_does_not_take():
    # 62.1% at 60¢ is about 0.4¢ after the fee.
    decision = _decide(p_up=0.621, ask_up=0.60, ask_down=0.45, ask_down_size=0)
    assert decision.take_side is None


def test_a_stale_book_is_not_taken():
    decision = _decide(up_book_age_seconds=2.0)
    assert decision.take_side is None


def test_quotes_sit_three_cents_through_the_model():
    decision = _decide(p_up=0.50, ask_up=0.50, ask_down=0.50, ask_up_size=1, ask_down_size=1)
    assert decision.take_side is None
    assert decision.bid_up == 0.47
    assert decision.ask_up == 0.53
    assert decision.bid_down == 0.47
    assert decision.ask_down == 0.53
    assert decision.split_ok is True


def test_split_is_refused_when_the_bids_sum_over_99_cents():
    # With a zero hurdle both bids sit at 50¢. 50 + 50 = 100¢ > 99¢: no split.
    decision = decide(
        0.50,
        ask_up=None,
        ask_up_size=0,
        ask_down=None,
        ask_down_size=0,
        fee_rate=0.07,
        min_edge=0.0,
        clip=5,
        up_book_age_seconds=0,
        down_book_age_seconds=0,
    )
    assert decision.bid_up == 0.50
    assert decision.bid_down == 0.50
    assert decision.split_ok is False


def test_missing_fair_value_skips():
    decision = _decide(p_up=None)
    assert decision.reason == "no fair value"
    assert decision.bid_up is None


def test_weekend_and_quiet_utc_morning_use_a_wider_hurdle():
    saturday = datetime(2026, 11, 7, 15, 0, tzinfo=timezone.utc)
    quiet = datetime(2026, 11, 9, 10, 30, tzinfo=timezone.utc)
    busy = datetime(2026, 11, 9, 14, 0, tzinfo=timezone.utc)
    assert min_edge(saturday) == 0.05
    assert min_edge(quiet) == 0.05
    assert min_edge(busy) == 0.03


def test_slug_is_the_window_open():
    # 2026-11-09 07:37:00 UTC is inside the 07:35 window.
    unix = datetime(2026, 11, 9, 7, 37, tzinfo=timezone.utc).timestamp()
    assert window_start(unix) == 1794209700
    assert slug_for("btc", unix) == "btc-updown-5m-1794209700"


def test_gamma_payload_is_read_from_the_market_object():
    event = {
        "slug": "btc-updown-5m-1794209700",
        "markets": [
            {
                "slug": "btc-updown-5m-1794209700",
                "question": "Bitcoin Up or Down",
                "conditionId": "0xabc",
                "outcomes": '["Down", "Up"]',
                "clobTokenIds": '["down-token", "up-token"]',
                "feeSchedule": {"rate": 0.07, "takerOnly": True, "rebateRate": 0.2},
                "cryptoMarketConfig": {
                    "asset": "btc",
                    "duration": "5m",
                    "twapLookbackSeconds": 60,
                },
                "orderMinSize": 5,
                "orderPriceMinTickSize": 0.01,
                "negRisk": False,
                "version": "v1",
            }
        ],
    }
    market = parse_event(event)
    assert market.up_token == "up-token"
    assert market.down_token == "down-token"
    assert market.fee_rate == 0.07
    assert market.rebate_rate == 0.2
    assert market.lookback_seconds == 60
    assert market.version == "v1"
    assert market.min_size == 5
