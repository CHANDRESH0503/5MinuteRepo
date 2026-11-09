import json

from fivemin.dashboard import Tail
from fivemin.report import phase1, state
from synthetic import generate


def test_a_single_settlement_mismatch_stops_the_build():
    g = phase1(generate(n_windows=4, mismatch_at=2))
    assert g["settlement"]["mismatched"] == 1
    assert g["settlement"]["status"] == "stop"


def test_gates_stay_pending_below_fifty_windows():
    g = phase1(generate(n_windows=4))
    assert g["settlement"]["matched"] == 4 and g["settlement"]["status"] == "pending"
    assert g["brier"]["n"] == 4 and g["brier"]["model"] is not None
    assert g["cap"]["status"] == "pass"


def test_a_capped_probability_breach_is_a_stop():
    recs = generate(n_windows=1)
    tick = next(r for r in recs if r["type"] == "tick" and r["seconds_left"] > 200)
    tick["p_up"] = 0.97
    assert phase1(recs)["cap"]["status"] == "stop"


def test_state_is_flagged_synthetic_and_shows_takes():
    s = state(generate(n_windows=2))
    assert s["synthetic"] is True
    assert any(r["takes"] for r in s["rows"])


def test_tail_reads_only_complete_lines(tmp_path):
    log = tmp_path / "x.jsonl"
    log.write_text(json.dumps({"type": "tick"}) + "\n" + '{"type": "ti')
    tail = Tail(log)
    assert len(tail.read()) == 1
    with log.open("a") as fh:
        fh.write('ck"}\n')
    assert len(tail.read()) == 2
