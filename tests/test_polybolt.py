"""Frames shaped like the PolyBolt AsyncAPI examples (docs read 9 Nov 2026)."""

from fivemin.polybolt import ChainlinkState, subscribe_frame

T0 = 1794211200  # a window open


def live(channel, seq, second, value, source="chainlink", **extra):
    return {"v": 1, "channel": channel, "seq": seq, "ts": second * 1000,
            "payload": {"symbol": "btcusd", "value": float(value), "full_accuracy_value": str(value),
                        "timestamp": second * 1000, "source": source, **extra}}


def test_subscribe_pins_chainlink_and_the_60s_window():
    subs = subscribe_frame()["subscriptions"]
    assert subs[0] == {"channel": "price.crypto", "filter": {"symbol": "btcusd", "provider": "chainlink"}}
    assert subs[1]["filter"]["window_seconds"] == 60


def test_non_chainlink_points_are_rejected_not_priced():
    st = ChainlinkState()
    st.on_envelope(live("price.crypto", 1, T0, "100000.5", source="pyth"), received=T0)
    st.on_envelope(live("price.crypto", 2, T0 + 1, "100001.25"), received=T0 + 1)
    assert st.spot_by_second == {T0 + 1: 100001.25}
    assert st.rejected == {"source pyth": 1}


def test_snapshot_batch_uses_the_batch_source():
    st = ChainlinkState()
    env = {"v": 1, "channel": "price.crypto", "seq": 1, "ts": 0, "snapshot": True,
           "payload": {"symbol": "btcusd", "source": "chainlink",
                       "data": [{"timestamp": (T0 - 2) * 1000, "value": 1.0, "full_accuracy_value": "99999.5"},
                                {"timestamp": (T0 - 1) * 1000, "value": 1.0, "full_accuracy_value": "99999.75"}]}}
    st.on_envelope(env, received=T0)
    assert st.spot_by_second == {T0 - 2: 99999.5, T0 - 1: 99999.75}
    pyth = {**env, "payload": {**env["payload"], "source": "pyth"}}
    st2 = ChainlinkState()
    st2.on_envelope(pyth, received=T0)
    assert st2.spot_by_second == {}


def test_k_is_the_twap_print_on_the_bell_or_nothing():
    st = ChainlinkState()
    st.on_envelope(live("price.crypto.twap", 1, T0 - 1, "100000", window_seconds=60), received=T0)
    assert st.twap60_at(T0) is None  # no print on the bell: skip, never substitute
    st.on_envelope(live("price.crypto.twap", 2, T0, "100010.5", window_seconds=60), received=T0)
    assert st.twap60_at(T0) == 100010.5
    st.on_envelope(live("price.crypto.twap", 3, T0 + 1, "1", window_seconds=30), received=T0)
    assert st.rejected == {"twap window 30": 1}


def test_stale_spot_is_none():
    st = ChainlinkState()
    st.on_envelope(live("price.crypto", 1, T0, "100000"), received=T0)
    assert st.spot(now=T0 + 0.5) == 100000.0
    assert st.spot(now=T0 + 1.5) is None


def test_seq_gap_and_dropped_are_holes():
    st = ChainlinkState()
    st.on_envelope(live("price.crypto", 1, T0, "1"), received=T0)
    st.on_envelope(live("price.crypto", 3, T0 + 1, "1"), received=T0)
    st.on_envelope({**live("price.crypto", 4, T0 + 2, "1"), "dropped": 2}, received=T0)
    assert [h[1] for h in st.holes] == [3, 4]


def test_final_minute_needs_every_second():
    st = ChainlinkState()
    end = T0 + 300
    for i, s in enumerate(range(end - 60, end - 30)):
        st.on_envelope(live("price.crypto", i + 1, s, "100000"), received=s)
    assert len(st.final_minute_prices(end, now=end - 30)) == 30
    del st.spot_by_second[end - 45]
    assert st.final_minute_prices(end, now=end - 30) is None


def test_minute_spots_stop_at_a_hole():
    st = ChainlinkState()
    for m in range(70):
        st.spot_by_second[T0 - 60 * m] = 100000.0 + m
    assert len(st.minute_spots(now=T0 + 5)) == 61
    del st.spot_by_second[T0 - 60 * 10]
    assert len(st.minute_spots(now=T0 + 5)) == 10
