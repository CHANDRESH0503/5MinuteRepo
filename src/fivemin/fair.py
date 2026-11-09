"""Fair P(Up) for a market that settles on a 60-second average.

With more than 60 seconds left, the variance of that average for a driftless
random walk is sigma^2 * (R - 2L/3). L is 60, so the time term is R - 40
seconds. Inside the last minute the seconds already printed are locked, and
only the unfinished part of the average is still random.

The formula is for a 60-second lookback. Other lookbacks return None so the
caller skips the window instead of using the wrong variance.

Above 120 seconds left the probability is capped at 95%. A 34,559-window
replay showed the uncapped normal saying 99.7% when the favorite won about 95%.
"""

from __future__ import annotations

from math import erf, isfinite, log, sqrt

LOOKBACK_SECONDS = 60
VARIANCE_OFFSET_SECONDS = 40.0  # 2 * 60 / 3


def normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + erf(z / sqrt(2.0)))


def _cap(probability: float, seconds_left: float, cap: float, cap_after_seconds: float) -> float:
    if seconds_left > cap_after_seconds:
        return min(max(probability, 1.0 - cap), cap)
    return min(max(probability, 0.0), 1.0)


def fair_up(
    spot: float,
    price_to_beat: float,
    sigma_1m: float,
    seconds_left: float,
    *,
    lookback_seconds: int = LOOKBACK_SECONDS,
    final_minute_prices: list[float] | None = None,
    cap: float = 0.95,
    cap_after_seconds: float = 120.0,
) -> float | None:
    """P(close TWAP >= price_to_beat).

    `sigma_1m` is the standard deviation of recent one-minute log returns.
    `final_minute_prices` is required once `seconds_left` is 60 or less: one
    price per second already inside the averaging window, oldest first.
    Returns None when the formula does not apply.
    """
    if lookback_seconds != LOOKBACK_SECONDS:
        return None
    if not all(isfinite(x) for x in (spot, price_to_beat, sigma_1m, seconds_left)):
        return None
    if spot <= 0.0 or price_to_beat <= 0.0 or sigma_1m <= 0.0:
        return None
    if seconds_left < 0.0:
        return None

    if seconds_left > LOOKBACK_SECONDS:
        horizon_seconds = max(seconds_left - VARIANCE_OFFSET_SECONDS, 1.0)
        z = log(spot / price_to_beat) / (sigma_1m * sqrt(horizon_seconds / 60.0))
        probability = normal_cdf(z)
        return _cap(probability, seconds_left, cap, cap_after_seconds)

    if final_minute_prices is None:
        return None

    printed = list(final_minute_prices)
    if len(printed) > LOOKBACK_SECONDS:
        return None
    if any(not isfinite(price) or price <= 0.0 for price in printed):
        return None
    # One print per completed second. A list shorter than the elapsed part of
    # the averaging window has a hole; a longer one has a duplicate. Skip both.
    if abs(len(printed) - (LOOKBACK_SECONDS - seconds_left)) > 1.0:
        return None

    remaining = LOOKBACK_SECONDS - len(printed)
    if remaining == 0:
        average = sum(printed) / LOOKBACK_SECONDS
        probability = 1.0 if average >= price_to_beat else 0.0
        return _cap(probability, seconds_left, cap, cap_after_seconds)

    required = (price_to_beat * LOOKBACK_SECONDS - sum(printed)) / remaining
    # Convert a per-minute log volatility into a price move per sqrt(second).
    sigma_price = spot * sigma_1m / sqrt(60.0)
    sd = sigma_price * sqrt(remaining / 3.0)
    if sd <= 0.0:
        probability = 1.0 if spot >= required else 0.0
    else:
        probability = normal_cdf((spot - required) / sd)
    return _cap(probability, seconds_left, cap, cap_after_seconds)


def brier(pairs: list[tuple[float, int]]) -> float | None:
    """Mean squared error of probabilities against 0/1 outcomes."""
    if not pairs:
        return None
    return sum((probability - outcome) ** 2 for probability, outcome in pairs) / len(pairs)


SIGMA_RETURNS = 60


def sigma_1m(minute_prices: list[float]) -> float | None:
    """Sample std of one-minute log returns of Chainlink spot, trailing hour.

    `minute_prices` are spot at consecutive completed minutes, oldest first.
    The last 61 prices give the 60 returns. Fewer, a non-positive price, or a
    non-positive deviation returns None, which means no fair value.
    """
    prices = list(minute_prices)[-(SIGMA_RETURNS + 1):]
    if len(prices) < SIGMA_RETURNS + 1:
        return None
    if any(not isfinite(p) or p <= 0.0 for p in prices):
        return None
    returns = [log(b / a) for a, b in zip(prices, prices[1:])]
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    sd = sqrt(var)
    return sd if sd > 0.0 else None
