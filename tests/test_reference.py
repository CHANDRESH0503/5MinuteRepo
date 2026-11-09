from fivemin.reference import SpotProxy, coinbase_mid, kraken_mid, sigma_from_candles


def test_proxy_is_the_median_and_goes_stale():
    p = SpotProxy()
    assert p.record({"coinbase": 100.0, "kraken": 102.0}, received=10.0) == 101.0
    assert p.spot(10.5) == 101.0 and p.spot(11.5) is None
    assert p.record({"coinbase": None, "kraken": 99.0}, received=11.0) == 99.0
    assert p.record({"coinbase": None, "kraken": None}, received=12.0) is None


def test_final_minute_needs_every_second():
    p = SpotProxy()
    end = 1000
    for s in range(end - 60, end):
        p.record({"x": 100.0}, received=s + 0.1)
    assert len(p.final_minute_prices(end, now=end)) == 60
    assert p.rebuilt_twap(end) == 100.0
    del p.by_second[end - 7]
    assert p.final_minute_prices(end, now=end) is None


def test_exchange_payloads():
    assert coinbase_mid({"bid": "100", "ask": "102"}) == 101.0
    assert kraken_mid({"error": [], "result": {"XXBTZUSD": {"b": ["100", "1", "1"], "a": ["101", "1", "1"]}}}) == 100.5
    assert kraken_mid({"error": ["EGeneral"]}) is None


def test_sigma_refuses_a_missing_minute():
    bars = {60 * i: {"open": 1, "high": 1, "low": 1, "close": 100.0 + (i % 2)} for i in range(100)}
    assert sigma_from_candles(bars, now=60 * 90 + 5) is not None
    del bars[60 * 70]
    assert sigma_from_candles(bars, now=60 * 90 + 5) is None


def test_sigma_tolerates_publication_lag_but_not_a_stale_feed():
    bars = {60 * i: {"open": 1, "high": 1, "low": 1, "close": 100.0 + (i % 2)} for i in range(62)}
    assert sigma_from_candles(bars, now=60 * 62 + 150) is not None  # newest bar ~2.5 min old
    assert sigma_from_candles(bars, now=60 * 62 + 400) is None  # older than five minutes
