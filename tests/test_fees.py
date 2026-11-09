from fivemin.fees import buy_edge, fee_per_share, fee_usdc, pair_cost, taker_breakeven


def test_fee_at_fifty_cents_is_one_point_seven_five_cents():
    assert fee_per_share(0.50, 0.07) == 0.0175
    assert abs(fee_usdc(100, 0.50, 0.07) - 1.75) < 1e-9


def test_fee_is_symmetric_around_fifty_cents():
    assert abs(fee_per_share(0.30, 0.07) - fee_per_share(0.70, 0.07)) < 1e-12


def test_taker_breakeven_at_fifty_cents():
    assert abs(taker_breakeven(0.50, 0.07) - 0.5175) < 1e-12


def test_worked_example_edges():
    # A 62.1% model at 55¢ clears. The same model at 60¢ does not.
    at_55 = buy_edge(0.621, 0.55, 0.07)
    at_60 = buy_edge(0.621, 0.60, 0.07)
    assert at_55 > 0.03
    assert at_60 < 0.03


def test_pair_cost_over_a_dollar_is_not_a_lock():
    assert pair_cost(0.52, 0.52, 0.07) > 1.0
    assert pair_cost(0.40, 0.40, 0.07) < 1.0
