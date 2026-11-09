"""Reduce the append-only log to the Phase 1 gates and the dashboard state.

Everything the dashboard shows comes from `state(records)`. Nothing is kept
outside the log, so any number on screen can be rebuilt from the file alone:

    PYTHONPATH=src python -m fivemin.report logs/phase1.jsonl
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from fivemin.fair import brier

GATE_WINDOWS = 50
BRIER_STOP = 0.02
SCORE_SECONDS_LEFT = 240.0  # 60 s after the open: K exists, before the last minute
CAP_SECONDS = 120.0
AMBIGUOUS_BPS = 1.0  # candle TWAP estimates were within 0.5 bp on 9 Nov; inside 1 bp a call is not judged


def load(path: Path) -> list[dict[str, Any]]:
    out = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    out.append({"type": "corrupt", "raw": line[:200]})
    return out


def _score_tick(ticks: list[dict]) -> dict | None:
    """The first tick at or after the predeclared second (seconds_left <= 240)."""
    eligible = [t for t in ticks if t["seconds_left"] <= SCORE_SECONDS_LEFT]
    if not eligible:
        return None
    first = max(eligible, key=lambda t: t["seconds_left"])
    return first if SCORE_SECONDS_LEFT - first["seconds_left"] <= 2.0 else None


def phase1(records: list[dict]) -> dict[str, Any]:
    ticks_by_open: dict[int, list[dict]] = {}
    for r in records:
        if r.get("type") == "tick":
            ticks_by_open.setdefault(r["open"], []).append(r)
    resolutions = [r for r in records if r.get("type") == "resolution" and r.get("official")]

    matched = mismatched = no_local = 0
    model_pairs, mid_pairs = [], []
    for r in resolutions:
        if r.get("local_call") is None:
            no_local += 1
        elif r["local_call"] == r["official"]:
            matched += 1
        else:
            mismatched += 1
        tick = _score_tick(ticks_by_open.get(r["open"], []))
        if tick and tick.get("p_up") is not None and tick["up"].get("mid") is not None:
            y = 1 if r["official"] == "up" else 0
            model_pairs.append((tick["p_up"], y))
            mid_pairs.append((tick["up"]["mid"], y))

    def med_abs(key: str) -> float | None:
        vals = sorted(abs(r[key]) for r in resolutions if r.get(key) is not None)
        return None if not vals else vals[len(vals) // 2]

    judged = [r for r in resolutions if r.get("candle_call") and r.get("candle_margin_bps") is not None
              and abs(r["candle_margin_bps"]) > AMBIGUOUS_BPS]
    candle_disagree = [r["slug"] for r in judged if r["candle_call"] != r["official"]]
    basis = [r["basis_bps"] for r in records if r.get("type") == "basis" and r.get("basis_bps") is not None]

    cap_violations = [
        {"slug": t["slug"], "seconds_left": t["seconds_left"], "p_up": t["p_up"]}
        for ts in ticks_by_open.values() for t in ts
        if t.get("p_up") is not None and t["seconds_left"] > CAP_SECONDS and not 0.05 - 1e-12 <= t["p_up"] <= 0.95 + 1e-12
    ]
    b_model, b_mid = brier(model_pairs), brier(mid_pairs)
    diff = None if b_model is None or b_mid is None else b_model - b_mid

    def gate(passed: bool, stopped: bool) -> str:
        return "stop" if stopped else ("pass" if passed else "pending")

    return {
        "resolved": len(resolutions),
        "settlement": {
            "matched": matched, "mismatched": mismatched, "no_local_call": no_local,
            "status": gate(matched >= GATE_WINDOWS and mismatched == 0, mismatched > 0),
        },
        "brier": {
            "n": len(model_pairs), "model": b_model, "mid": b_mid, "diff": diff,
            "status": gate(len(model_pairs) >= GATE_WINDOWS and diff is not None and diff <= BRIER_STOP,
                           diff is not None and diff > BRIER_STOP),
        },
        "cap": {"violations": cap_violations[:20], "count": len(cap_violations),
                "status": "stop" if cap_violations else "pass"},
        "independent": {
            "judged": len(judged), "disagree": candle_disagree,
            "ambiguous": sum(1 for r in resolutions if r.get("candle_margin_bps") is not None
                             and abs(r["candle_margin_bps"]) <= AMBIGUOUS_BPS),
            "median_K_err_bps": med_abs("candle_K_err_bps"),
            "median_close_err_bps": med_abs("candle_close_err_bps"),
            "status": "stop" if candle_disagree else ("pass" if len(judged) >= GATE_WINDOWS else "pending"),
        },
        "proxy": {
            "median_close_err_bps": med_abs("proxy_close_err_bps"),
            "basis_n": len(basis),
            "basis_median_bps": None if not basis else sorted(basis)[len(basis) // 2],
            "basis_max_abs_bps": None if not basis else max(abs(b) for b in basis),
        },
        "target": GATE_WINDOWS,
    }


def _series_point(t: dict) -> dict:
    d = t.get("decision") or {}
    return {
        "s": t["seconds_left"], "p": t.get("p_up"),
        "mu": t["up"].get("mid"), "md": t["down"].get("mid"),
        "bu": t["up"].get("bid"), "au": t["up"].get("ask"),
        "bd": t["down"].get("bid"), "ad": t["down"].get("ask"),
        "qbu": d.get("bid_up"), "qau": d.get("ask_up"),
        "qbd": d.get("bid_down"), "qad": d.get("ask_down"),
        "take": d.get("take_side"), "S": t.get("S"), "K": t.get("K"),
    }


def state(records: list[dict]) -> dict[str, Any]:
    ticks = [r for r in records if r.get("type") == "tick"]
    windows = {r["open"]: r for r in records if r.get("type") == "window"}
    resolutions = {r["open"]: r for r in records if r.get("type") == "resolution"}
    last = ticks[-1] if ticks else None
    current = [t for t in ticks if last and t["open"] == last["open"]]

    rows = []
    by_open: dict[int, list[dict]] = {}
    for t in ticks:
        by_open.setdefault(t["open"], []).append(t)
    for open_unix in sorted(set(windows) | set(by_open), reverse=True)[:24]:
        ts = by_open.get(open_unix, [])
        w, res = windows.get(open_unix, {}), resolutions.get(open_unix, {})
        score = _score_tick(ts)
        reasons = Counter((t.get("decision") or {}).get("reason", "?") for t in ts)
        rows.append({
            "open": open_unix, "slug": w.get("slug") or (ts[0]["slug"] if ts else None),
            "ticks": len(ts), "skip_reasons": w.get("skip_reasons", []),
            "K": res.get("K"), "close_twap": res.get("close_twap"),
            "local": res.get("local_call"), "official": res.get("official"),
            "p_at_score": score.get("p_up") if score else None,
            "mid_at_score": score["up"].get("mid") if score else None,
            "takes": sum(1 for t in ts if (t.get("decision") or {}).get("take_side")),
            "candle_call": res.get("candle_call"), "candle_margin_bps": res.get("candle_margin_bps"),
            "proxy_close_err_bps": res.get("proxy_close_err_bps"),
            "reasons": dict(reasons),
        })

    fills = [r for r in records if r.get("type") == "fill"]
    reasons_now = Counter((t.get("decision") or {}).get("reason", "?") for t in current)
    return {
        "generated_from": len(records),
        "synthetic": any(r.get("synthetic") for r in records[:5]),
        "last": last,
        "window": windows.get(last["open"]) if last else None,
        "series": [_series_point(t) for t in current],
        "reasons_now": dict(reasons_now),
        "rows": rows,
        "phase1": phase1(records),
        "errors": [r for r in records if r.get("type") in ("error", "corrupt")][-8:],
        "error_count": sum(1 for r in records if r.get("type") in ("error", "corrupt")),
        "paper": {"engine": "running" if fills else "not running", "fills": len(fills)},
    }


def main() -> None:
    s = state(load(Path(sys.argv[1])))
    print(json.dumps({"phase1": s["phase1"], "rows": s["rows"][:5], "paper": s["paper"],
                      "error_count": s["error_count"]}, indent=2, default=str))


if __name__ == "__main__":
    main()
