"""Limitless BTC 5-minute Up/Down market, as `GET /markets/{slug}` returns it.

Read on 9 Nov 2026 from https://api.limitless.exchange (docs.limitless.exchange):

  * slug `btc-up-or-down-5-min-{unix_open}`; `startAt` is the open,
    `expirationTimestamp` (ms) the close.
  * `tokens.yes` is Up and `tokens.no` is Down: a resolved window carried
    `chainlinkTwapResolution.outcome: "UP"` with `winningOutcomeIndex: 0`.
  * Settlement is the Chainlink BTC/USD 60 s TWAP
    (`metadata.chainlinkDataStream.streamType: "twap"`, `twapWindowSeconds: 60`).
    `metadata.openPrice` is the price to beat; `chainlinkTwapResolution.close.raw`
    (18 decimals) is the close. Up when close >= open.
  * Taker fee on new venues (`venue.feeModuleType == "COLLATERAL"`, `metadata.fee`):
    rate_bps * N * (S - N) / (10000 * S), standard rate 500 bps. Makers pay zero.
  * `settings.minSize` is the LP-rewards minimum, not an order minimum. No tick
    is published; book prices come in thousandths, so a 1-cent grid is valid.

A missing field is None and `skip_reasons` refuses the window.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

WINDOW_SECONDS = 300
SLUG_PREFIX = "btc-up-or-down-5-min-"
REQUIRED_LOOKBACK = 60  # fair.LOOKBACK_SECONDS
STANDARD_FEE_BPS = 500  # docs: "The standard rate is 5% (500 bps)"


def window_start(unix_seconds: float) -> int:
    return int(unix_seconds) // WINDOW_SECONDS * WINDOW_SECONDS


def slug_for(unix_seconds: float) -> str:
    return f"{SLUG_PREFIX}{window_start(unix_seconds)}"


def _unix(iso: Any) -> int | None:
    if not iso:
        return None
    return int(datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp())


def _price(value: Any, *, raw18: bool = False) -> float | None:
    """Decimal string to float. Placeholders are None: before the open the
    venue sends openPrice "---" (seen 9 Nov 2026)."""
    if value in (None, ""):
        return None
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return None
    if not d.is_finite() or d <= 0:
        return None
    return float(d / Decimal(10) ** 18) if raw18 else float(d)


@dataclass(frozen=True)
class CryptoWindow:
    slug: str
    condition_id: str
    up_token: str
    down_token: str
    open_unix: int | None
    end_unix: int | None
    fee_rate: float | None  # per-share curve coefficient, 0.05 at 500 bps
    rebate_rate: float | None
    lookback_seconds: int | None
    stream_type: str | None
    reward_min_shares: float | None
    price_to_beat: float | None  # metadata.openPrice, Chainlink TWAP at the open
    status: str | None
    winning_index: int | None
    close_twap: float | None  # chainlinkTwapResolution.close.raw / 1e18
    resolution_outcome: str | None  # "UP" / "DOWN" as the venue wrote it
    title: str = ""


def parse_market(m: dict[str, Any]) -> CryptoWindow:
    tokens = m.get("tokens") or {}
    if not tokens.get("yes") or not tokens.get("no"):
        raise ValueError("market has no yes/no tokens")
    md = m.get("metadata") or {}
    stream = md.get("chainlinkDataStream") or {}
    venue = m.get("venue") or {}
    settings = m.get("settings") or {}
    res = md.get("chainlinkTwapResolution") or {}

    fee_rate = None
    if md.get("fee") is False:
        fee_rate = 0.0
    elif md.get("fee") is True and venue.get("feeModuleType") == "COLLATERAL":
        fee_rate = STANDARD_FEE_BPS / 10_000
    reward_min = settings.get("minSize", md.get("minSize"))
    exp = m.get("expirationTimestamp")
    return CryptoWindow(
        slug=str(m.get("slug") or ""),
        condition_id=str(m.get("conditionId") or ""),
        up_token=str(tokens["yes"]),
        down_token=str(tokens["no"]),
        open_unix=_unix(m.get("startAt")),
        end_unix=None if exp is None else int(exp) // 1000,
        fee_rate=fee_rate,
        rebate_rate=None if settings.get("rebateRate") is None else float(settings["rebateRate"]),
        lookback_seconds=None if stream.get("twapWindowSeconds") is None else int(stream["twapWindowSeconds"]),
        stream_type=stream.get("streamType"),
        reward_min_shares=None if reward_min is None else float(reward_min) / 1e6,
        price_to_beat=_price(md.get("openPrice")),
        status=m.get("status"),
        winning_index=m.get("winningOutcomeIndex"),
        close_twap=_price((res.get("close") or {}).get("raw"), raw18=True),
        resolution_outcome=res.get("outcome"),
        title=str(m.get("title") or ""),
    )


def skip_reasons(window: CryptoWindow) -> list[str]:
    """Every reason to skip this window. Empty means it can be priced."""
    reasons: list[str] = []
    if not window.slug.startswith(SLUG_PREFIX):
        reasons.append(f"slug {window.slug!r} is not {SLUG_PREFIX}*")
    else:
        tail = window.slug[len(SLUG_PREFIX):]
        if not tail.isdigit() or int(tail) % WINDOW_SECONDS:
            reasons.append(f"slug open {tail!r} is not a 300-second boundary")
        elif window.open_unix is not None and int(tail) != window.open_unix:
            reasons.append(f"slug open {tail} != startAt {window.open_unix}")
    if window.open_unix is not None and window.end_unix is not None and window.end_unix - window.open_unix != WINDOW_SECONDS:
        reasons.append(f"window length {window.end_unix - window.open_unix} s")
    if window.stream_type != "twap":
        reasons.append(f"oracle stream {window.stream_type!r} is not twap")
    if window.lookback_seconds != REQUIRED_LOOKBACK:
        reasons.append(f"lookback {window.lookback_seconds!r} is not 60")
    if window.fee_rate is None:
        reasons.append("fee schedule unknown (not a COLLATERAL venue)")
    return reasons


def official_outcome(window: CryptoWindow) -> str | None:
    """'up' / 'down' once Limitless has resolved, else None (50-50 is None too)."""
    if window.status != "RESOLVED":
        return None
    return {0: "up", 1: "down"}.get(window.winning_index)


def local_call(close_twap: float, price_to_beat: float) -> str:
    """Our own settlement read. A tie resolves Up."""
    return "up" if close_twap >= price_to_beat else "down"
