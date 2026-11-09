"""When the residual hurdle is raised.

Weekends and 09:00-12:00 UTC are the quiet stretches in the 34,559-window
replay: the median 5-minute move was a few basis points, and a taker at 50¢
needs 51.75% just to cover the fee. Those hours use a wider residual.
"""

from __future__ import annotations

from datetime import datetime, timezone

BASE_EDGE = 0.03
QUIET_EDGE = 0.05


def min_edge(now: datetime, *, base: float = BASE_EDGE, quiet: float = QUIET_EDGE) -> float:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    if now.weekday() >= 5:
        return quiet
    if 9 <= now.hour < 12:
        return quiet
    return base
