"""Phase 1 read-only recorder. Places nothing, simulates nothing.

    PYTHONPATH=src python -m fivemin.recorder --out logs/phase1.jsonl --clip 5

Appends one JSON object per second, plus one `window` record at discovery and
one `resolution` record once Polymarket has resolved the window.

What is wired here, from the public endpoints checked on 9 Nov 2026:
  * Gamma discovery  https://gamma-api.polymarket.com/events?slug=...
  * CLOB book        https://clob.polymarket.com/book?token_id=...  (REST poll,
    a stand-in for the market websocket; book age is our receipt age)
  * Official outcome Gamma `closed`, `umaResolutionStatus`, `outcomePrices`

What is NOT wired: the Chainlink reference on PolyBolt. `ReferenceFeed` is the
seam. With `NullFeed`, S, K and sigma are null, `fair_up` is never called and
every record says "no fair value". That is the honest state, not a bug. A
PolyBolt adapter must fill `spot`, `twap60`, `minute_spots` and
`final_minute_prices` from `full_accuracy_value`, and log raw frames.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from fivemin.book import Book, parse_book
from fivemin.fair import fair_up, sigma_1m
from fivemin.market import (
    WINDOW_SECONDS,
    CryptoWindow,
    official_outcome,
    parse_event,
    skip_reasons,
    slug_for,
    window_start,
)
from fivemin.session import min_edge
from fivemin.strategy import decide

GAMMA = "https://gamma-api.polymarket.com/events?slug={slug}"
CLOB_BOOK = "https://clob.polymarket.com/book?token_id={token}"
UA = {"User-Agent": "fivemin-recorder/0.1 (read-only)"}
DEPTH = 8


class ReferenceFeed(Protocol):
    name: str

    def spot(self) -> float | None: ...
    def twap60_at(self, unix: int) -> float | None: ...
    def minute_spots(self) -> list[float]: ...
    def final_minute_prices(self, window_end: int, now: float) -> list[float] | None: ...


class NullFeed:
    name = "chainlink not wired"

    def spot(self) -> float | None:
        return None

    def twap60_at(self, unix: int) -> float | None:
        return None

    def minute_spots(self) -> list[float]:
        return []

    def final_minute_prices(self, window_end: int, now: float) -> list[float] | None:
        return None


def _get(url: str, timeout: float = 5.0) -> Any:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def fetch_window(slug: str, *, closed: bool = False) -> tuple[CryptoWindow, dict] | None:
    url = GAMMA.format(slug=slug) + ("&closed=true" if closed else "")
    events = _get(url)
    if not events:
        return None
    return parse_event(events[0]), events[0]["markets"][0]


def fetch_book(token: str) -> Book:
    raw = _get(CLOB_BOOK.format(token=token))
    return parse_book(raw, received=time.time())


def book_summary(book: Book | None, now: float) -> dict[str, Any]:
    if book is None:
        return {"bid": None, "bid_size": 0, "ask": None, "ask_size": 0, "mid": None, "age": None}
    bb, ba = book.best_bid, book.best_ask
    return {
        "bid": bb[0] if bb else None,
        "bid_size": bb[1] if bb else 0,
        "ask": ba[0] if ba else None,
        "ask_size": ba[1] if ba else 0,
        "mid": book.mid(),
        "age": round(book.age(now), 3),
        "venue_ts": book.timestamp,
        "bids": book.bids[:DEPTH],
        "asks": book.asks[:DEPTH],
    }


class Recorder:
    def __init__(self, out: Path, clip: float, feed: ReferenceFeed) -> None:
        self.out = out
        self.clip = clip
        self.feed = feed
        self.windows: dict[int, CryptoWindow] = {}
        self.k: dict[int, float | None] = {}
        self.pending: dict[int, float] = {}  # open -> next poll time for the outcome
        out.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, record: dict[str, Any]) -> None:
        with self.out.open("a") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")

    def discover(self, open_unix: int) -> CryptoWindow | None:
        if open_unix in self.windows:
            return self.windows[open_unix]
        slug = slug_for("btc", open_unix)
        try:
            got = fetch_window(slug)
        except Exception as exc:  # noqa: BLE001 - log and retry next second
            self.emit({"type": "error", "t": time.time(), "slug": slug, "where": "gamma", "error": repr(exc)})
            return None
        if got is None:
            return None
        window, raw = got
        self.windows[open_unix] = window
        self.emit({
            "type": "window",
            "t": time.time(),
            "slug": window.slug,
            "open": open_unix,
            "question": window.question,
            "condition_id": window.condition_id,
            "up_token": window.up_token,
            "down_token": window.down_token,
            "raw_fee_schedule": raw.get("feeSchedule"),
            "raw_crypto_config": raw.get("cryptoMarketConfig"),
            "fee_type": raw.get("feeType"),
            "min_size": window.min_size,
            "tick": window.tick,
            "lookback": window.lookback_seconds,
            "version": window.version,
            "neg_risk": window.neg_risk,
            "skip_reasons": skip_reasons(window, clip=self.clip),
            "reference_feed": self.feed.name,
        })
        return window

    def tick(self, now: float) -> None:
        open_unix = window_start(now)
        end = open_unix + WINDOW_SECONDS
        window = self.discover(open_unix)
        if end - now <= 30:
            self.discover(end)  # next window, about 30 s before its open
        if window is None:
            return
        self.pending.setdefault(open_unix, end + 15)

        if open_unix not in self.k:
            self.k[open_unix] = self.feed.twap60_at(open_unix)  # the print on the bell, or None

        books: dict[str, Book | None] = {}
        for side, token in (("up", window.up_token), ("down", window.down_token)):
            try:
                books[side] = fetch_book(token)
            except Exception as exc:  # noqa: BLE001
                books[side] = None
                self.emit({"type": "error", "t": now, "slug": window.slug, "where": f"book {side}", "error": repr(exc)})
        now = time.time()
        seconds_left = end - now
        up, down = book_summary(books["up"], now), book_summary(books["down"], now)

        spot, k = self.feed.spot(), self.k[open_unix]
        sigma = sigma_1m(self.feed.minute_spots())
        p_up = None
        skips = skip_reasons(window, clip=self.clip)
        if not skips and spot is not None and k is not None and sigma is not None:
            p_up = fair_up(
                spot, k, sigma, seconds_left,
                lookback_seconds=window.lookback_seconds,
                final_minute_prices=self.feed.final_minute_prices(end, now) if seconds_left <= 60 else None,
            )
        edge = min_edge(datetime.fromtimestamp(now, tz=timezone.utc))
        decision = decide(
            p_up,
            ask_up=up["ask"], ask_up_size=up["ask_size"],
            ask_down=down["ask"], ask_down_size=down["ask_size"],
            fee_rate=window.fee_rate, min_edge=edge, clip=self.clip,
            up_book_age_seconds=up["age"] if up["age"] is not None else float("inf"),
            down_book_age_seconds=down["age"] if down["age"] is not None else float("inf"),
        )
        self.emit({
            "type": "tick",
            "t": round(now, 3),
            "slug": window.slug,
            "open": open_unix,
            "seconds_left": round(seconds_left, 3),
            "S": spot, "K": k, "sigma_1m": sigma, "p_up": p_up,
            "up": up, "down": down,
            "min_edge": edge,
            "fee_rate": window.fee_rate,
            "skip_reasons": skips,
            "reference_feed": self.feed.name,
            "decision": asdict(decision),  # what decide() said; nothing is placed or filled
        })

    def resolve_due(self, now: float) -> None:
        for open_unix, due in list(self.pending.items()):
            if now < due:
                continue
            slug = slug_for("btc", open_unix)
            try:
                got = fetch_window(slug, closed=True) or fetch_window(slug)
            except Exception:  # noqa: BLE001
                got = None
            official = official_outcome(got[0]) if got else None
            if official is None:
                self.pending[open_unix] = now + 20
                if now - open_unix > 3600:
                    self.emit({"type": "resolution", "t": now, "slug": slug, "open": open_unix,
                               "official": None, "note": "not resolved after an hour"})
                    del self.pending[open_unix]
                continue
            close = self.feed.twap60_at(open_unix + WINDOW_SECONDS)
            k = self.k.get(open_unix)
            local = None if close is None or k is None else ("up" if close >= k else "down")
            self.emit({
                "type": "resolution", "t": now, "slug": slug, "open": open_unix,
                "K": k, "close_twap": close, "local_call": local, "official": official,
                "reference_feed": self.feed.name,
            })
            del self.pending[open_unix]

    def run(self, seconds: float | None = None) -> None:
        started = time.time()
        while seconds is None or time.time() - started < seconds:
            beat = time.time()
            self.tick(beat)
            self.resolve_due(time.time())
            time.sleep(max(0.0, 1.0 - (time.time() - beat)))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=Path("logs/phase1.jsonl"))
    ap.add_argument("--clip", type=float, required=True, help="config, must be >= orderMinSize")
    ap.add_argument("--seconds", type=float, default=None)
    args = ap.parse_args()
    Recorder(args.out, args.clip, NullFeed()).run(args.seconds)


if __name__ == "__main__":
    main()
