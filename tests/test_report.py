"""Request metrics, think lag, SLO, cost, headline, rendering and compare (SPEC_B §9.3-9.7)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import make_dict

from personaload.config import scenario_from_dict
from personaload.report.aggregate import (
    MS,
    Window,
    request_metrics,
    reused_conversations,
    think_lag,
)
from personaload.report.compare import ReportError, compare_markdown, load_run
from personaload.report.judge import (
    NO_PASSING_STAGE,
    headline,
    judge_stage,
    slo_verdict,
    stage_warnings,
    usd_per_million_out,
    usd_per_thousand_turns,
)
from personaload.report.records import Record
from personaload.report.render import render_markdown, run_report, stage_table
from personaload.workload.sessions import SessionPlan, UserTurn

SCENARIO = scenario_from_dict(make_dict())  # slo: ttft_p95 1s, itl_p95 100ms, error_rate_max 0.01


def _r(start_ms, end_ms, *, error=False, code=None, session=None, conv=None, turn=None, **m):
    ns = lambda ms: None if ms is None else round(ms * MS)  # noqa: E731
    return Record(conv, session, turn, None, ns(start_ms), ns(end_ms), error, code, **m)


WINDOW = Window(0, 1000 * MS, 2000 * MS)  # [W, E_eff) = [1000 ms, 2000 ms), 1 s


def _golden_records():
    return [
        _r(1100, 1500, ttft_ms=100, e2e_ms=400, itl_ms=(20, 30), prompt_tokens=100,
           output_tokens=3, cached_tokens=40),
        # ends after E_eff: latency yes, throughput no; no cached field
        _r(1200, 2100, ttft_ms=300, e2e_ms=900, itl_ms=(10,), prompt_tokens=200),
        _r(1300, 1300, error=True, code=500),
        # started before W: throughput only; no usage -> 3 chunk gaps + 1 = 4 tokens
        _r(900, 1600, ttft_ms=50, e2e_ms=700, itl_ms=(1, 2, 3)),
        _r(None, None, error=True),  # synthesized record without timestamps: unplaceable
        _r(1400, 1400, error=True),  # code-less error
        # no usage and < 2 chunks -> 1 token
        _r(1500, 1900, ttft_ms=200, e2e_ms=400),
    ]  # fmt: skip


def test_request_metrics_golden():
    m = request_metrics(_golden_records(), WINDOW, drain_cancelled=0)
    assert m["requests"] == {"success": 3, "error": 2}
    assert m["error_rate"] == pytest.approx(2 / 5)
    assert m["error_codes"] == {"500": 1, "null": 1}
    assert m["req_per_s"] == pytest.approx(3.0)  # ends in window: 1500, 1600, 1900
    assert m["out_tok_per_s"] == pytest.approx(3 + 4 + 1)
    assert m["usage_missing"] == 2
    assert m["ttft_ms"] == pytest.approx({"p50": 200, "p90": 280, "p95": 290, "p99": 298})
    assert m["itl_ms"]["p50"] == 20 and m["itl_ms"]["p99"] == pytest.approx(29.8)  # pooled
    assert m["e2e_ms"]["p95"] == pytest.approx(850)
    assert m["cache_hit"] == {"rate": pytest.approx(40 / 300), "reported": 1, "eligible": 2}


def test_latency_counts_starts_and_throughput_counts_ends_in_half_open_window():
    records = [
        _r(1100, 1500, output_tokens=3),  # starts and ends inside
        _r(950, 1050, output_tokens=2),  # ends inside only
        _r(900, 1400, output_tokens=1),  # ends inside only
        _r(1200, 2100, output_tokens=5),  # starts inside only
        _r(1000, 1000, error=True),  # starts exactly at W: inside
        _r(2000, 2000, error=True),  # starts exactly at E_eff: outside
    ]
    m = request_metrics(records, WINDOW, 0)
    assert m["requests"] == {"success": 2, "error": 1}
    assert m["req_per_s"] == pytest.approx(3) and m["out_tok_per_s"] == pytest.approx(6)


def test_drain_cancellations_count_as_errors():
    m = request_metrics(_golden_records(), WINDOW, drain_cancelled=2)
    assert m["error_rate"] == pytest.approx((2 + 2) / (5 + 2))


def test_cache_hit_is_na_without_any_cached_field_and_error_rate_without_requests():
    m = request_metrics([_r(1100, 1200, prompt_tokens=10, output_tokens=1)], WINDOW, None)
    assert m["cache_hit"] == {"rate": None, "reported": 0, "eligible": 1}
    empty = request_metrics([], WINDOW, None)
    assert empty["error_rate"] is None and empty["ttft_ms"]["p50"] is None
    assert empty["req_per_s"] == 0 and empty["out_tok_per_s"] == 0


def test_all_errors_degenerate_case():
    m = request_metrics([_r(1100, 1100, error=True, code=500)] * 3, WINDOW, 0)
    assert m["error_rate"] == 1.0 and m["ttft_ms"]["p95"] is None and m["out_tok_per_s"] == 0
    verdict = slo_verdict(m, SCENARIO.slo)
    assert verdict == {"verdict": "FAIL", "violations": ["error_rate"]}
    assert usd_per_million_out(m["out_tok_per_s"], SCENARIO.cost) is None


def test_think_lag_golden():
    plans = {
        "A": SessionPlan(
            "A", 0, (UserTurn("a", 1), UserTurn("b", 1, 100.0), UserTurn("c", 1, 200.0))
        )
    }
    records = [
        _r(1000, 1100, session="x", conv="A", turn=0),
        _r(1210.5, 1300, session="x", conv="A", turn=1),  # lag 1210.5 - 1100 - 100 = 10.5
        _r(1494, 1600, session="x", conv="A", turn=2),  # lag 1494 - 1300 - 200 = -6
        _r(1000, 1000, session="y", conv="A", turn=0, error=True),
        _r(1200, 1300, session="y", conv="A", turn=1),  # after an error turn: excluded
        _r(500, 600, session="z", conv="A", turn=0),
        _r(900, 950, session="z", conv="A", turn=1),  # starts before W: not a pair
        _r(1100, 1200, session="u", conv="B", turn=0),  # B is played once: no reuse
    ]
    lag = think_lag(records, plans, Window(0, 1100 * MS, 2000 * MS))
    assert lag == {
        "p50": pytest.approx(2.25), "p95": pytest.approx(9.675), "p99": pytest.approx(10.335),
        "pairs": 2, "excluded_after_error": 1, "below_clock_floor": 1,
    }  # fmt: skip
    assert reused_conversations(records) == 1  # only A (replayed as x, y, z)


def test_think_lag_floor_is_minus_5ms_and_missing_field_records_are_skipped():
    two_turns = (UserTurn("a", 1), UserTurn("b", 1, 100.0))
    plans = {"A": SessionPlan("A", 0, two_turns)}
    records = [
        _r(1100, 1200, session="w", conv="A", turn=0),
        _r(1297, 1400, session="w", conv="A", turn=1),  # lag -3: above the -5 ms floor
        _r(1100, 1200, session="v", conv="A", turn=0),
        _r(1300, 1400, session="v", conv="A", turn=1),  # lag 0
        _r(1500, 1500, error=True),  # AIPerf fallback record: no session, no turn
    ]
    lag = think_lag(records, plans, WINDOW)
    assert lag["pairs"] == 2 and lag["below_clock_floor"] == 0
    assert stage_warnings({"think_lag_ms": {"p99": 100.0}}) == []  # warn only above 100 ms


# --- SLO, cost, headline, warnings -------------------------------------------------------


@pytest.mark.parametrize(
    ("ttft", "itl", "err", "expected"),
    [
        (1000, 100, 0.01, {"verdict": "PASS", "violations": []}),  # limits are inclusive
        (1000.1, 50, 0.0, {"verdict": "FAIL", "violations": ["ttft_p95"]}),
        (None, 101, None, {"verdict": "FAIL", "violations": ["itl_p95"]}),
        (900, None, 0.0, {"verdict": "N/A", "violations": []}),
    ],
)
def test_slo_verdict(ttft, itl, err, expected):
    metrics = {"ttft_ms": {"p95": ttft}, "itl_ms": {"p95": itl}, "error_rate": err}
    assert slo_verdict(metrics, SCENARIO.slo) == expected


def test_usd_per_million_out():
    cost = scenario_from_dict(make_dict(cost={"gpu_hourly_usd": 1.09, "gpu_count": 2})).cost
    assert usd_per_million_out(1000.0, cost) == pytest.approx(1.09 * 2 / (1000 * 3600) * 1e6)
    assert usd_per_million_out(None, cost) is None


def test_cost_per_turn_and_session():
    cost = scenario_from_dict(make_dict(cost={"gpu_hourly_usd": 1.09, "gpu_count": 2})).cost
    per_1k = 1.09 * 2 / (4.0 * 3600) * 1000  # 4 turns/s -> GPU-seconds per turn x 1000
    assert usd_per_thousand_turns(4.0, cost) == pytest.approx(per_1k)
    assert usd_per_thousand_turns(0.0, cost) is None
    judged = judge_stage({"req_per_s": 4.0, "out_tok_per_s": 400.0}, SCENARIO.slo, cost, 12.5)
    assert judged["usd_per_1k_turns"] == pytest.approx(per_1k)
    assert judged["usd_per_session"] == pytest.approx(per_1k / 1000 * 12.5)
    empty = judge_stage({"req_per_s": 0.0}, SCENARIO.slo, cost, 12.5)
    assert empty["usd_per_1k_turns"] is None and empty["usd_per_session"] is None


def _st(n, status="ok", verdict="PASS", tps=100.0, **extra):
    usd = None if not tps else 1e6 / (tps * 3600)
    slo = {"verdict": verdict, "violations": []}
    return {"stage": n, "status": status, "slo": slo, "out_tok_per_s": tps, "usd_per_1m_out": usd,
            "mode": "concurrency", "config": {"concurrency": 10}, **extra}  # fmt: skip


def test_headline_picks_fastest_passing_complete_stage_lowest_number_on_ties():
    stages = [_st(1, tps=100), _st(2, tps=300), _st(3, tps=300), _st(4, "partial", tps=900),
              _st(5, verdict="FAIL", tps=1000), _st(6, "failed", tps=2000)]  # fmt: skip
    assert headline(stages) == {
        "stage": 2, "out_tok_per_s": 300, "usd_per_1m_out": pytest.approx(1e6 / (300 * 3600)),
        "usd_per_1k_turns": None, "usd_per_session": None,
    }  # fmt: skip
    assert headline([_st(1, verdict="FAIL"), _st(2, verdict="N/A")]) == {
        "stage": None, "message": NO_PASSING_STAGE
    }  # fmt: skip


def test_stage_warnings():
    summary = {
        "status": "ok", "drain_cancelled": 3, "usage_missing": 4, "reused_conversations": 2,
        "cpu_warnings": 5, "active_sessions": {"unobserved": 1},
        "think_lag_ms": {"p99": 150.0, "below_clock_floor": 2, "excluded_after_error": 7},
        "user_cancelled": 6,
    }  # fmt: skip
    text = "\n".join(stage_warnings(summary))
    for part in ("3 requests cancelled after the drain",
                 "no usage: output tokens counted from chunks (4 requests)",
                 "dataset reused", "5 AIPerf worker CPU warnings", "1 session instances",
                 "think lag p99 150ms", "7 pairs after an error turn excluded",
                 "clock skew suspected",
                 "6 in-flight requests cancelled by the interrupt"):  # fmt: skip
        assert part in text, part
    assert stage_warnings({"status": "ok", "drain_cancelled": 0}) == []
    assert (
        "drain cancellations unknown"
        in stage_warnings({"status": "ok", "drain_cancelled": None})[0]
    )


# --- rendering and compare ---------------------------------------------------------------


def _full(n, **kw):
    values = {
        "active_sessions": {"avg": 9.996, "min": 10, "max": 10},
        "req_per_s": 12.345,
        "ttft_ms": {"p50": 201.4, "p90": 240, "p95": 250.6, "p99": 300.2},
        "itl_ms": {"p50": 25.04, "p90": 25.2, "p95": 25.31, "p99": 26},
        "e2e_ms": {"p95": 1234.4},
        "error_rate": 0.0123,
        "cache_hit": {"rate": 0.78, "reported": 12, "eligible": 13},
        "drain_cancelled": 0,
        "dataset_sha256": "aa",
        "usd_per_1k_turns": 0.0225,
        "usd_per_session": 0.225,
    }
    return _st(n, **(values | kw))


def test_stage_table_concurrency_row():
    table = stage_table([(None, _full(1))])
    header, _, row = table.splitlines()
    assert header.startswith("| stage | conc | active sess | req/s | out tok/s |")
    assert row == (
        "| 01 | 10 | 10.0 (10–10) | 12.35 | 100.0 | 201/251/300 | 25.0/25.3 | 1234 "
        "| 1.2% | 78.0% (cached reported 12/13) | PASS | $2.778 | $0.0225 | $0.225000 |"
    )


def test_stage_table_arrival_censored_partial_and_na():
    arrival = {
        "rate": 5,
        "actual_rate": 4.4,
        "max_concurrency": 10,
        "queued": 11,
        "not_started": 77,
        "start_lag_ms": {"p50": 12000.6, "p95": 17747.6, "p99": 17877.8},
    }
    s = _full(2, status="partial", verdict="N/A", tps=0, mode="arrival_rate", arrival=arrival)
    s |= {"drain_cancelled": 4, "cache_hit": {"rate": None, "reported": 0, "eligible": 0}}
    s |= {"usd_per_1k_turns": None, "usd_per_session": None}
    header, _, row = stage_table([(None, s)]).splitlines()
    assert "| sess/s |" in header
    assert header.endswith("| start lag p50/p95/p99 | queued | not started |")
    assert row.startswith("| 02 (partial) | 5 / 4.40 (10) |")
    assert "201/251/300 (4 censored, lower bound)" in row
    assert row.endswith("| N/A | N/A | N/A | N/A | N/A | 12001/17748/17878 | 11 | 77 |")


def test_run_report_and_markdown():
    failed = {"stage": 3, "status": "failed", "error": "aiperf exit code 2\nlog tail"}
    report = run_report(
        "demo-1",
        [_full(1, warnings=["no usage: output tokens counted from chunks (4 requests)"]), failed],
    )
    assert report["headline"]["stage"] == 1
    assert report["warnings"] == [
        "stage 01: no usage: output tokens counted from chunks (4 requests)",
        "stage 03: failed — aiperf exit code 2",
    ]
    md = render_markdown(report)
    assert "# personaload report: demo-1" in md
    assert "stage 01: $2.778/1M out, $0.0225/1K turns, $0.225000/session (out tok/s" in md
    assert "| 03 (failed) |" in md and "- stage 03: failed — aiperf exit code 2" in md
    only_fail = run_report("x", [_full(1, verdict="FAIL")])
    assert NO_PASSING_STAGE in render_markdown(only_fail)


def _write_run(root: Path, name: str, stages: list[dict]) -> Path:
    for s in stages:
        sdir = root / name / f"stage_{s['stage']:02d}"
        sdir.mkdir(parents=True)
        (sdir / "stage.json").write_text(json.dumps(s))
    return root / name


def test_compare_reads_stage_json_only_and_flags_different_workloads(tmp_path):
    a = _write_run(tmp_path, "naive", [_full(1), _full(2, dataset_sha256="bb")])
    b = _write_run(tmp_path, "vllm", [_full(1, tps=300), _full(2, dataset_sha256="cc")])
    md = compare_markdown([a, b])
    assert "stage 2 ran a different workload" in md and "stage 1 ran" not in md
    assert "- naive: stage 01: $2.778/1M out" in md and "- vllm: stage 01:" in md
    assert "| naive | 01 |" in md and "| vllm | 02 |" in md
    assert [s["stage"] for s in load_run(a)] == [1, 2]
    with pytest.raises(ReportError, match="stage.json"):
        load_run(tmp_path / "missing")


def test_compare_rejects_foreign_stage_json_and_names_dot_by_its_directory(tmp_path, monkeypatch):
    bad = _write_run(tmp_path, "bad", [_full(1)])
    (bad / "stage_01" / "stage.json").write_text("{}")
    with pytest.raises(ReportError, match="not a stage.json"):
        load_run(bad)
    run = _write_run(tmp_path, "c50-run", [_full(1)])
    monkeypatch.chdir(run)
    assert "- c50-run: stage 01" in compare_markdown([Path(".")])


def test_report_on_a_parent_folder_suggests_a_run_directory(tmp_path):
    _write_run(tmp_path, "smoke-20261016-000000", [_full(1)])
    with pytest.raises(ReportError, match=r"pass a run directory, e\.g\. .*smoke-20261016-000000"):
        load_run(tmp_path)
