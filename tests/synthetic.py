"""Seeded synthetic log for UI and gate tests. Every record says synthetic.

Not market data. Spot is a random walk; books sit around the model with a
deliberate mispricing so decide() produces takes. Run:

    PYTHONPATH=src python tests/synthetic.py logs/synthetic.jsonl
"""

import json
import random
import sys
from dataclasses import asdict
from math import exp, sqrt

from fivemin.fair import fair_up
from fivemin.strategy import decide

OPEN0 = 1794211200
FEE = 0.07


def _book(p, rng):
    ask = min(0.99, max(0.01, round(p + rng.choice([0.01, 0.02, -0.06]), 2)))
    bid = round(max(0.01, ask - 0.01), 2)
    asks = [(round(ask + i / 100, 2), 50 + 30 * i) for i in range(8) if ask + i / 100 <= 0.99]
    bids = [(round(bid - i / 100, 2), 60 + 25 * i) for i in range(8) if bid - i / 100 >= 0.01]
    return asks, bids


def side(asks, bids, age):
    return {"bid": bids[0][0] if bids else None, "bid_size": bids[0][1] if bids else 0,
            "ask": asks[0][0] if asks else None, "ask_size": asks[0][1] if asks else 0,
            "mid": (bids[0][0] + asks[0][0]) / 2 if bids and asks else None, "age": age,
            "bids": bids, "asks": asks}


def generate(n_windows=6, seed=7, mismatch_at=None):
    rng = random.Random(seed)
    sigma = 0.0008
    spot = 100_000.0
    out = []
    for w in range(n_windows):
        o = OPEN0 + 300 * w
        slug = f"btc-updown-5m-{o}"
        out.append({"type": "window", "synthetic": True, "t": o, "slug": slug, "open": o, "skip_reasons": [],
                    "reference_feed": "synthetic"})
        k = spot
        last_minute = []
        for e in range(300):
            spot *= exp(rng.gauss(0, sigma / sqrt(60)))
            left = 300 - e
            if left <= 60:
                last_minute.append(spot)
            p = fair_up(spot, k, sigma, left, lookback_seconds=60,
                        final_minute_prices=last_minute[:-1] if left <= 60 else None)
            pu = 0.5 if p is None else p
            ua, ub = _book(pu, rng)
            da = [(round(1 - px, 2), s) for px, s in ub]
            db = [(round(1 - px, 2), s) for px, s in ua]
            up, down = side(ua, ub, 0.2), side(da, db, 0.2)
            d = decide(p, ask_up=up["ask"], ask_up_size=up["ask_size"], ask_down=down["ask"],
                       ask_down_size=down["ask_size"], fee_rate=FEE, min_edge=0.03, clip=5,
                       up_book_age_seconds=0.2, down_book_age_seconds=0.2)
            out.append({"type": "tick", "synthetic": True, "t": o + e, "slug": slug, "open": o,
                        "seconds_left": float(left), "S": spot, "K": k, "sigma_1m": sigma, "p_up": p,
                        "up": up, "down": down, "min_edge": 0.03, "fee_rate": FEE, "skip_reasons": [],
                        "reference_feed": "synthetic", "decision": asdict(d)})
        close = sum(last_minute) / len(last_minute)
        local = "up" if close >= k else "down"
        official = local if w != mismatch_at else ("down" if local == "up" else "up")
        out.append({"type": "resolution", "synthetic": True, "t": o + 320, "slug": slug, "open": o, "K": k,
                    "close_twap": close, "local_call": local, "official": official})
    return out


if __name__ == "__main__":
    with open(sys.argv[1], "w") as fh:
        for r in generate(n_windows=int(sys.argv[2]) if len(sys.argv) > 2 else 6):
            fh.write(json.dumps(r) + "\n")
