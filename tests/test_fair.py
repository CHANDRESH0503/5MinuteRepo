from fivemin.fair import brier, fair_up


def test_spot_on_the_price_to_beat_is_a_coin_flip():
    p = fair_up(100_000, 100_000, sigma_1m=0.0004, seconds_left=300)
    assert p is not None
    assert abs(p - 0.5) < 1e-9


def test_a_large_early_lead_is_capped_at_95_percent():
    p = fair_up(101_000, 100_000, sigma_1m=0.0002, seconds_left=240)
    assert p == 0.95


def test_the_cap_lifts_inside_two_minutes():
    p = fair_up(101_000, 100_000, sigma_1m=0.0002, seconds_left=90, final_minute_prices=None)
    # 90 seconds is still before the averaging window, so no final-minute list is required.
    assert p is not None
    assert p > 0.95


def test_other_lookbacks_are_refused():
    assert fair_up(100, 100, 0.001, 200, lookback_seconds=30) is None


def test_last_minute_without_prints_is_refused():
    assert fair_up(100, 100, 0.001, 30) is None


def test_a_locked_average_above_the_strike_is_up():
    prices = [100.0] * 60
    p = fair_up(100, 99, sigma_1m=0.001, seconds_left=0, final_minute_prices=prices)
    assert p == 1.0


def test_a_locked_average_below_the_strike_is_down():
    prices = [100.0] * 60
    p = fair_up(100, 101, sigma_1m=0.001, seconds_left=0, final_minute_prices=prices)
    assert p == 0.0


def test_partial_average_uses_only_the_unfinished_seconds():
    # Fifty seconds already printed far above the strike. Ten seconds left.
    printed = [110.0] * 50
    p = fair_up(110, 100, sigma_1m=0.0004, seconds_left=10, final_minute_prices=printed)
    assert p is not None
    assert p > 0.99


def test_brier_of_a_perfect_forecast_is_zero():
    assert brier([(1.0, 1), (0.0, 0)]) == 0.0
    assert brier([]) is None
