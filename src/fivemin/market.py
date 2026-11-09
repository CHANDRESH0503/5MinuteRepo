"""Gamma event parsing for a 5-minute crypto Up/Down market.

Token ids are matched by outcome name. The fee rate and the TWAP lookback
come off the market object. A missing field is left as None, never replaced by
the 9 Nov 2026 value, and `skip_reasons` then refuses the window.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

WINDOW_SECONDS = 300
SLUG_PREFIX = "btc-updown-5m-"
REQUIRED_TICK = 0.01  # strategy.TICK; decide() takes no tick argument
REQUIRED_LOOKBACK = 60  # fair.LOOKBACK_SECONDS
FIVE_MINUTE_DURATIONS = {"5m", "5min", "300", "300s"}


def window_start(unix_seconds: float) -> int:
    return int(unix_seconds) // WINDOW_SECONDS * WINDOW_SECONDS


def slug_for(asset: str, unix_seconds: float) -> str:
    return f"{asset}-updown-5m-{window_start(unix_seconds)}"


def _loads(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _unix(iso: Any) -> int | None:
    if not iso:
        return None
    return int(datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp())


@dataclass(frozen=True)
class CryptoWindow:
    slug: str
    condition_id: str
    question: str
    version: str | None
    neg_risk: bool
    up_token: str
    down_token: str
    fee_rate: float
    rebate_rate: float
    taker_only: bool | None
    lookback_seconds: int | None
    min_size: float | None
    tick: float | None
    asset: str | None
    duration: str | None
    fee_exponent: float | None = None
    open_unix: int | None = None
    end_unix: int | None = None
    closed: bool = False
    resolution_status: str | None = None
    up_price: float | None = None
    down_price: float | None = None


def parse_event(event: dict[str, Any]) -> CryptoWindow:
    markets = event.get("markets") or []
    if not markets:
        raise ValueError("event has no markets")
    market = markets[0]
    outcomes = [str(name) for name in _loads(market["outcomes"])]
    token_ids = [str(token) for token in _loads(market["clobTokenIds"])]
    if len(outcomes) != len(token_ids):
        raise ValueError("outcomes and clobTokenIds differ in length")
    by_name = {name.lower(): token for name, token in zip(outcomes, token_ids)}
    if "up" not in by_name or "down" not in by_name:
        raise ValueError(f"expected Up and Down outcomes, got {outcomes}")

    prices_raw = market.get("outcomePrices")
    prices: dict[str, float] = {}
    if prices_raw is not None:
        values = [float(p) for p in _loads(prices_raw)]
        if len(values) == len(outcomes):
            prices = {name.lower(): value for name, value in zip(outcomes, values)}

    fee = market.get("feeSchedule") or {}
    if "rate" not in fee:
        raise ValueError("feeSchedule.rate is missing")
    config = market.get("cryptoMarketConfig") or {}
    lookback = config.get("twapLookbackSeconds")
    taker_only = fee.get("takerOnly")
    return CryptoWindow(
        slug=str(market.get("slug") or event.get("slug") or ""),
        condition_id=str(market["conditionId"]),
        question=str(market.get("question") or ""),
        version=None if market.get("version") is None else str(market.get("version")),
        neg_risk=bool(market.get("negRisk")),
        up_token=by_name["up"],
        down_token=by_name["down"],
        fee_rate=float(fee["rate"]),
        rebate_rate=float(fee.get("rebateRate") or 0.0),
        taker_only=None if taker_only is None else bool(taker_only),
        lookback_seconds=None if lookback is None else int(lookback),
        min_size=_float_or_none(market.get("orderMinSize")),
        tick=_float_or_none(market.get("orderPriceMinTickSize")),
        asset=None if config.get("asset") is None else str(config.get("asset")),
        duration=None if config.get("duration") is None else str(config.get("duration")),
        fee_exponent=_float_or_none(fee.get("exponent")),
        open_unix=_unix(market.get("eventStartTime") or event.get("startTime")),
        end_unix=_unix(market.get("endDate")),
        closed=bool(market.get("closed")),
        resolution_status=market.get("umaResolutionStatus"),
        up_price=prices.get("up"),
        down_price=prices.get("down"),
    )


def skip_reasons(window: CryptoWindow, *, clip: float) -> list[str]:
    """Every reason the plan gives to skip this window. Empty means tradeable.

    Nothing here substitutes the 9 Nov 2026 value for a missing field.
    """
    reasons: list[str] = []
    if not window.slug.startswith(SLUG_PREFIX):
        reasons.append(f"slug {window.slug!r} is not {SLUG_PREFIX}*")
    else:
        slug_open = window.slug[len(SLUG_PREFIX):]
        if not slug_open.isdigit() or int(slug_open) % WINDOW_SECONDS:
            reasons.append(f"slug open {slug_open!r} is not a 300-second boundary")
        elif window.open_unix is not None and int(slug_open) != window.open_unix:
            reasons.append(f"slug open {slug_open} != eventStartTime {window.open_unix}")
    if window.asset is not None and window.asset.lower() != "btc":
        reasons.append(f"asset {window.asset!r}")
    if window.duration is not None and window.duration.lower() not in FIVE_MINUTE_DURATIONS:
        reasons.append(f"duration {window.duration!r}")
    if window.fee_exponent != 1:
        reasons.append(f"fee exponent {window.fee_exponent!r} is not 1")
    if window.taker_only is not True:
        reasons.append(f"takerOnly {window.taker_only!r} is not true")
    if window.tick is None or abs(window.tick - REQUIRED_TICK) > 1e-12:
        reasons.append(f"tick {window.tick!r} is not 0.01")
    if window.lookback_seconds != REQUIRED_LOOKBACK:
        reasons.append(f"lookback {window.lookback_seconds!r} is not 60")
    if window.min_size is None:
        reasons.append("orderMinSize missing")
    elif clip < window.min_size:
        reasons.append(f"clip {clip} < orderMinSize {window.min_size}")
    if window.fee_rate < 0:
        reasons.append(f"fee rate {window.fee_rate} < 0")
    return reasons


def official_outcome(window: CryptoWindow) -> str | None:
    """'up' or 'down' once Polymarket has resolved the market, else None."""
    if not window.closed or window.resolution_status != "resolved":
        return None
    if window.up_price == 1.0 and window.down_price == 0.0:
        return "up"
    if window.up_price == 0.0 and window.down_price == 1.0:
        return "down"
    return None


def local_call(close_twap: float, price_to_beat: float) -> str:
    """Our own settlement read. A tie resolves Up."""
    return "up" if close_twap >= price_to_beat else "down"
