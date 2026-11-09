"""Phase 1 read-only recorder for Limitless BTC 5-minute markets.

    PYTHONPATH=src python -m fivemin.recorder --out logs/phase1.jsonl --clip 5

Places nothing, simulates nothing. Appends one JSON object per second, one
`window` record at discovery, one `basis` record per minute and one
`resolution` record once Limitless has resolved the window.

Sources, all public and reachable from the India VPS (checked 9 Nov 2026):
  * Limitless  GET /markets/{slug}              market, K (openPrice), official result
               GET /markets/{slug}/orderbook    the Up (YES) book; Down is its mirror
               GET /markets/{slug}/oracle-candles  Chainlink BTC/USD spot, 1-minute
  * Coinbase and Kraken tickers                 live spot proxy (see reference.py)
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fivemin.book import Book, mirror, parse_book
from fivemin.fair import fair_up
from fivemin.market import (
    WINDOW_SECONDS,
    CryptoWindow,
    local_call,
    official_outcome,
    parse_market,
    skip_reasons,
    slug_for,
    window_start,
)
from fivemin.reference import SpotProxy, bps, fetch_candles, sigma_from_candles, twap_estimate
from fivemin.session import min_edge
from fivemin.strategy import decide

API = "https://api.limitless.exchange"
UA = {"User-Agent": "fivemin-recorder/0.2 (read-only)"}
DEPTH = 8
VENUE = "limitless"


def _get(url: str, timeout: float = 3.0) -> Any:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def fetch_market(slug: str) -> tuple[CryptoWindow, dict] | None:
    try:
        raw = _get(f"{API}/markets/{slug}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    if not isinstance(raw, dict) or "slug" not in raw:
        return None
    return parse_market(raw), raw


def fetch_book(slug: str) -> Book:
    return parse_book(_get(f"{API}/markets/{slug}/orderbook"), received=time.time())


def book_summary(book: Book | None, now: float) -> dict[str, Any]:
    if book is None:
        return {"bid": None, "bid_size": 0, "ask": None, "ask_size": 0, "mid": None, "age": None, "bids": [], "asks": []}
    bb, ba = book.best_bid, book.best_ask
    return {
        "bid": bb[0] if bb else None, "bid_size": bb[1] if bb else 0,
        "ask": ba[0] if ba else None, "ask_size": ba[1] if ba else 0,
        "mid": book.mid(), "age": round(book.age(now), 3),
        "bids": book.bids[:DEPTH], "asks": book.asks[:DEPTH],
    }


class Recorder:
    def __init__(self, out: Path, clip: float) -> None:
        self.out = out
        self.clip = clip
        self.spot = SpotProxy()
        self.spot.start()
        self.windows: dict[int, CryptoWindow] = {}
        self.pending: dict[int, float] = {}
        self.candles: dict[int, dict[str, float]] = {}
        self.candles_at = 0.0
        self.basis_done: set[int] = set()
        out.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, record: dict[str, Any]) -> None:
        record.setdefault("venue", VENUE)
        with self.out.open("a") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")

    def error(self, where: str, exc: Exception, slug: str | None = None) -> None:
        self.emit({"type": "error", "t": time.time(), "slug": slug, "where": where, "error": repr(exc)[:300]})

    # discovery ------------------------------------------------------------
    def discover(self, open_unix: int, *, refresh: bool = False) -> CryptoWindow | None:
        known = self.windows.get(open_unix)
        if known is not None and not refresh:
            return known
        slug = slug_for(open_unix)
        try:
            got = fetch_market(slug)
        except Exception as exc:  # noqa: BLE001
            self.error("market", exc, slug)
            return known
        if got is None:
            return known
        window, raw = got
        first = known is None
        self.windows[open_unix] = window
        if first or (known.price_to_beat is None and window.price_to_beat is not None):
            md = raw.get("metadata") or {}
            self.emit({
                "type": "window", "t": time.time(), "slug": window.slug, "open": open_unix,
                "title": window.title, "condition_id": window.condition_id,
                "up_token": window.up_token, "down_token": window.down_token,
                "K": window.price_to_beat, "fee_rate": window.fee_rate, "rebate_rate": window.rebate_rate,
                "lookback": window.lookback_seconds, "reward_min_shares": window.reward_min_shares,
                "raw_venue": raw.get("venue"), "raw_settings": raw.get("settings"),
                "raw_stream": md.get("chainlinkDataStream"), "taker_delay_ms": md.get("takerDelayMs"),
                "skip_reasons": skip_reasons(window),
            })
        return window

    # candles + basis --------------------------------------------------------
    def refresh_candles(self, now: float, slug: str) -> None:
        if now - self.candles_at < 60:
            return
        self.candles_at = now
        try:
            self.candles.update(fetch_candles(slug, int(now) - 66 * 60, int(now)))
        except Exception as exc:  # noqa: BLE001
            self.error("candles", exc, slug)
            return
        for t in [t for t in self.candles if t < now - 4 * 3600]:
            del self.candles[t]
        # Proxy basis: proxy price at the last second of each Chainlink bar vs the bar close.
        for bar_open, bar in sorted(self.candles.items())[-5:]:
            if bar_open in self.basis_done:
                continue
            proxy = self.spot.by_second.get(bar_open + 59)
            if proxy is None:
                continue
            self.basis_done.add(bar_open)
            self.emit({"type": "basis", "t": now, "minute": bar_open, "chainlink": bar["close"],
                       "proxy": proxy, "basis_bps": bps(proxy, bar["close"])})

    # one second -------------------------------------------------------------
    def tick(self, now: float) -> None:
        open_unix = window_start(now)
        end = open_unix + WINDOW_SECONDS
        window = self.windows.get(open_unix)
        if window is None or (window.price_to_beat is None and int(now) % 5 == 0):
            window = self.discover(open_unix, refresh=True)
        if end - now <= 30:
            self.discover(end)
        if window is None:
            return
        self.refresh_candles(now, window.slug)
        self.pending.setdefault(open_unix, end + 30)

        try:
            up_book: Book | None = fetch_book(window.slug)
        except Exception as exc:  # noqa: BLE001
            up_book = None
            self.error("book", exc, window.slug)
        now = time.time()
        seconds_left = end - now
        up = book_summary(up_book, now)
        down = book_summary(mirror(up_book) if up_book else None, now)

        spot = self.spot.spot(now)
        k = window.price_to_beat
        sigma = sigma_from_candles(self.candles, now)
        skips = skip_reasons(window)
        p_up = None
        if not skips and spot is not None and k is not None and sigma is not None:
            p_up = fair_up(
                spot, k, sigma, seconds_left,
                lookback_seconds=window.lookback_seconds,
                final_minute_prices=self.spot.final_minute_prices(end, now) if seconds_left <= 60 else None,
            )
        edge = min_edge(datetime.fromtimestamp(now, tz=timezone.utc))
        inf = float("inf")
        decision = decide(
            p_up,
            ask_up=up["ask"], ask_up_size=up["ask_size"],
            ask_down=down["ask"], ask_down_size=down["ask_size"],
            fee_rate=window.fee_rate if window.fee_rate is not None else 0.0,
            min_edge=edge, clip=self.clip,
            up_book_age_seconds=up["age"] if up["age"] is not None else inf,
            down_book_age_seconds=down["age"] if down["age"] is not None else inf,
        )
        self.emit({
            "type": "tick", "t": round(now, 3), "slug": window.slug, "open": open_unix,
            "seconds_left": round(seconds_left, 3),
            "S": spot, "K": k, "sigma_1m": sigma, "p_up": p_up,
            "spot_sources": self.spot.sources,
            "up": up, "down": down, "min_edge": edge, "fee_rate": window.fee_rate,
            "skip_reasons": skips, "reference_feed": self.spot.name,
            "decision": asdict(decision),  # what decide() said; nothing is placed or filled
        })
        self.spot.prune(now)

    # resolution -------------------------------------------------------------
    def resolve_due(self, now: float) -> None:
        for open_unix, due in list(self.pending.items()):
            if now < due:
                continue
            slug = slug_for(open_unix)
            end = open_unix + WINDOW_SECONDS
            try:
                got = fetch_market(slug)
            except Exception as exc:  # noqa: BLE001
                self.error("resolution", exc, slug)
                got = None
            official = official_outcome(got[0]) if got else None
            if official is None:
                self.pending[open_unix] = now + 30
                if now - end > 3 * 3600:
                    self.emit({"type": "resolution", "t": now, "slug": slug, "open": open_unix,
                               "official": None, "note": "not resolved after 3 hours",
                               "status": got[0].status if got else None})
                    del self.pending[open_unix]
                continue
            w = got[0]
            try:
                bars = fetch_candles(slug, open_unix - 180, end + 120)
            except Exception as exc:  # noqa: BLE001
                self.error("resolution candles", exc, slug)
                bars = {}
            k_est, close_est = twap_estimate(bars, open_unix), twap_estimate(bars, end)
            proxy_close = self.spot.rebuilt_twap(end)
            local = None if w.close_twap is None or w.price_to_beat is None else local_call(w.close_twap, w.price_to_beat)
            candle_call = None if k_est is None or close_est is None else local_call(close_est, k_est)
            self.emit({
                "type": "resolution", "t": now, "slug": slug, "open": open_unix,
                "K": w.price_to_beat, "close_twap": w.close_twap, "official": official,
                "venue_outcome": w.resolution_outcome,
                "local_call": local,  # from the venue's own Chainlink open and close values
                "candle_K": k_est, "candle_close": close_est, "candle_call": candle_call,
                "candle_K_err_bps": bps(k_est, w.price_to_beat),
                "candle_close_err_bps": bps(close_est, w.close_twap),
                "candle_margin_bps": bps(close_est, k_est),
                "proxy_close": proxy_close, "proxy_close_err_bps": bps(proxy_close, w.close_twap),
            })
            del self.pending[open_unix]

    def run(self, seconds: float | None = None) -> None:
        started = time.time()
        while seconds is None or time.time() - started < seconds:
            beat = time.time()
            try:
                self.tick(beat)
                self.resolve_due(time.time())
            except Exception as exc:  # noqa: BLE001 - one bad second must not stop the record
                self.error("loop", exc)
            time.sleep(max(0.0, 1.0 - (time.time() - beat)))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=Path("logs/phase1.jsonl"))
    ap.add_argument("--clip", type=float, required=True, help="shares per take; config")
    ap.add_argument("--seconds", type=float, default=None)
    args = ap.parse_args()
    Recorder(args.out, args.clip).run(args.seconds)


if __name__ == "__main__":
    main()
