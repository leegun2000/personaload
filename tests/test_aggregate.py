"""Records, cancellation log parsing, window, active sessions, arrival diagnostics (SPEC_B §9)."""

from __future__ import annotations

from pathlib import Path

import pytest

from personaload.report.aggregate import (
    MS,
    Window,
    active_sessions,
    arrival_diagnostics,
    measurement_window,
    percentile,
)
from personaload.report.records import Record, load_records, parse_cancellations
from personaload.workload.sessions import SessionPlan, UserTurn

FIXTURE = (
    Path(__file__).parent.parent / "testdata" / "aiperf-0.12.0" / "profile_export.sample.jsonl"
)


def _plan(cid: str, turns: int, start_ms: float | None = None) -> SessionPlan:
    first = UserTurn("hi", 5, None, start_ms)
    return SessionPlan(cid, 0, (first, *[UserTurn("x", 5, 100.0) for _ in range(turns - 1)]))


def _rec(cid, session, turn, credit_ms=None, start_ms=None, end_ms=None, error=False) -> Record:
    ns = lambda ms: None if ms is None else round(ms * MS)  # noqa: E731
    return Record(cid, session, turn, ns(credit_ms), ns(start_ms), ns(end_ms), error)


# --- records / logs ----------------------------------------------------------------------


def test_load_real_aiperf_records():
    records = load_records(FIXTURE)
    assert len(records) == 13
    assert sum(r.error for r in records) == 1
    ok = records[0]
    assert ok.conversation_id.startswith("s01-") and ok.session and ok.turn_index is not None
    assert ok.credit_issued_ns <= ok.request_start_ns <= ok.request_end_ns
    assert (ok.ttft_ms, ok.e2e_ms, ok.prompt_tokens, ok.output_tokens, ok.cached_tokens) == (
        202.20649999999998,
        502.085375,
        396,
        13,
        0,
    )
    assert len(ok.itl_ms) == 12 and ok.itl_ms[0] == 24.63225 and ok.status_code is None
    assert ok.aiperf_itl_ms == 24.989906249999997
    (err,) = [r for r in records if r.error]
    assert err.status_code == 500 and err.ttft_ms is None and err.itl_ms == ()


def test_load_records_tolerates_missing_fields_and_skips_other_phases(tmp_path):
    path = tmp_path / "p.jsonl"
    path.write_text(
        '{"metadata": {"benchmark_phase": "profiling"}, "metrics": {}, "error": {"code": 500}}\n'
        '{"metadata": {"benchmark_phase": "warmup", "turn_index": 0}, "metrics": {}}\n\n',
        encoding="utf-8",
    )
    (only,) = load_records(path)
    assert only == Record(None, None, None, None, None, None, True, status_code=500)


def test_load_records_skips_a_line_cut_short_by_sigkill(tmp_path):
    path = tmp_path / "p.jsonl"
    path.write_bytes(FIXTURE.read_bytes()[:-200])
    assert len(load_records(path)) == 12


@pytest.mark.parametrize(
    ("log", "drain", "user"),
    [
        (
            "... - NOTICE - Phase profiling (profiling) complete | completed=64, cancelled=0, "
            "errors=0 | sessions: completed=11, cancelled=3 | elapsed=12.77s",
            0,
            None,
        ),
        (
            "Phase profiling timed out, cancelling all credits. Stats: sent=9, completed=4, "
            "cancelled=0, in_flight=5\n"
            "Phase profiling (profiling) complete | completed=1,204, cancelled=1,005, errors=2 "
            "| sessions: completed=1, cancelled=9 | elapsed=5s | grace_period_timeout=True",
            1005,
            None,
        ),
        (
            "Phase profiling (profiling) complete | completed=10, cancelled=4, errors=0 | "
            "sessions: completed=2, cancelled=2 | elapsed=3s | was_cancelled=True",
            None,
            4,
        ),
        (  # Ctrl+C in AIPerf 0.12.0: no phase-complete line, only the router's cancel count
            "INFO - Sent cancellation requests for 1,017 in-flight credits across 10 workers\n"
            "... - NOTICE - Phase profiling (profiling) sending complete | sent=359, completed=342",
            None,
            1017,
        ),
        ("no phase line here", None, None),
    ],
)
def test_parse_cancellations(log, drain, user):
    parsed = parse_cancellations(log)
    assert (parsed.drain, parsed.user) == (drain, user)


# --- percentile / window -----------------------------------------------------------------


def test_percentile_linear():
    assert percentile([4, 1, 3, 2], 50) == 2.5
    assert percentile([1, 2, 3, 4], 95) == pytest.approx(3.85)
    assert percentile([7], 99) == 7
    assert percentile([], 50) is None


def test_concurrency_window():
    plans = {"a": _plan("a", 2)}
    records = [
        _rec("a", "x", 0, credit_ms=1000),
        _rec("a", "x", 1, credit_ms=1300),
        _rec(None, None, None),
    ]
    window = measurement_window(records, plans, 200, 500, None, arrival=False)
    assert window == Window(1000 * MS, 1200 * MS, 1700 * MS)
    stopped = measurement_window(records, plans, 200, 500, 1500 * MS, arrival=False)
    assert stopped.e_eff == 1500 * MS
    assert measurement_window(records, plans, 200, 500, 5000 * MS, False).e_eff == 1700 * MS
    assert measurement_window(records, plans, 200, 500, 1100 * MS, arrival=False).empty
    assert measurement_window([_rec(None, None, None)], plans, 0, 1, None, False) is None


def test_arrival_window_subtracts_schedule():
    plans = {"a": _plan("a", 1, 0.0), "b": _plan("b", 1, 500.0)}
    records = [_rec("a", "1", 0, credit_ms=10_020), _rec("b", "2", 0, credit_ms=10_505)]
    window = measurement_window(records, plans, 0, 1000, None, arrival=True)
    assert window.t0 == 10_005 * MS  # min(10020 - 0, 10505 - 500)


# --- active sessions ---------------------------------------------------------------------


def _active_fixture():
    plans = {"A": _plan("A", 2), "B": _plan("B", 2), "C": _plan("C", 1), "D": _plan("D", 2)}
    records = [
        _rec("A", "a", 0, credit_ms=0, start_ms=0, end_ms=20),
        _rec("A", "a", 1, credit_ms=30, start_ms=30, end_ms=40),  # A: [0, 40)
        _rec("B", "b", 0, credit_ms=0, start_ms=0, end_ms=20),  # B cut off: [0, ∞)
        _rec("C", "c", 0, credit_ms=40.5, start_ms=40.5, end_ms=150),  # C: hand-off 0.5 ms
        _rec("D", "d", 1, credit_ms=50, start_ms=50, end_ms=60),  # D: no turn 0 → unobserved
    ]
    return plans, records


def test_active_sessions_golden():
    plans, records = _active_fixture()
    active = active_sessions(records, plans, Window(0, 0, 100 * MS))
    # segments: [0,40)=2, [40,40.5)=1, [40.5,100)=2
    assert active.segments == ((0, 40 * MS, 2), (40 * MS, 40_500_000, 1), (40_500_000, 100 * MS, 2))
    assert active.avg == pytest.approx((40 * 2 + 0.5 * 1 + 59.5 * 2) / 100)
    assert (active.min, active.max, active.unobserved) == (2, 2, 1)  # 0.5 ms dip ignored


def test_active_sessions_respect_stop_and_warmup():
    plans, records = _active_fixture()
    stopped = active_sessions(records, plans, Window(0, 10 * MS, 50 * MS))
    assert stopped.avg == pytest.approx((30 * 2 + 0.5 * 1 + 9.5 * 2) / 40)


# --- arrival diagnostics -----------------------------------------------------------------


def test_arrival_diagnostics_golden():
    plans = {
        "s1": _plan("s1", 1, 0.0),
        "s2": _plan("s2", 1, 500.0),
        "s3": _plan("s3", 1, 1000.0),  # outside [warmup, warmup+duration)
        "s4": _plan("s4", 1, 200.0),  # never started
    }
    records = [
        _rec("s1", "1", 0, credit_ms=10_000, start_ms=10_000.5),
        _rec("s2", "2", 0, credit_ms=10_650, start_ms=10_650.5),
        _rec("s3", "3", 0, credit_ms=11_000, start_ms=11_001),
    ]
    window = measurement_window(records, plans, 0, 1000, None, arrival=True)
    diag = arrival_diagnostics(records, plans, window, 0, 1000)
    assert diag["target_sessions"] == 3 and diag["actual_rate"] == 3.0
    assert diag["start_lag_ms"]["p50"] == pytest.approx((0.5 + 150.5) / 2)
    assert diag["queued"] == 1 and diag["not_started"] == 1
    # warmup 100 ms: s1 (scheduled at 0) is excluded and s3 (at 1000) joins the targets
    window = measurement_window(records, plans, 100, 1000, None, arrival=True)
    diag = arrival_diagnostics(records, plans, window, 100, 1000)
    assert diag["target_sessions"] == 3 and diag["queued"] == 1 and diag["not_started"] == 1
    assert diag["start_lag_ms"]["p50"] == pytest.approx((1 + 150.5) / 2)
