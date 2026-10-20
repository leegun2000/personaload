"""Stage status decisions and stage.json fields without running AIPerf (SPEC_B §8.3-8.6)."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import signal
import socket
from datetime import datetime
from pathlib import Path

import pytest
from conftest import ARRIVAL_LOAD, make_dict

import personaload.runner.stages as stages
from personaload.config import scenario_from_dict
from personaload.runner.process import Interrupts, Outcome
from personaload.runner.stages import (
    RunResult,
    StageResult,
    _masked,
    _summarize,
    check_aiperf_version,
    check_prometheus_port,
    run_scenario,
    run_stage,
)
from personaload.workload.personas import load_personas

FIXTURE = (
    Path(__file__).parent.parent / "testdata" / "aiperf-0.12.0" / "profile_export.sample.jsonl"
)
DONE = "x - NOTICE - Phase profiling (profiling) complete | completed=12, cancelled=0, errors=1 | x"
# B4 acceptance run: mock --ttft 20ms --tps 400 --error-rate 0.1, concurrency 10, warmup 1s, 3s
ERRORS = FIXTURE.with_name("profile_export.errors.jsonl.gz")
ERRORS_DONE = (
    "x - NOTICE - Phase profiling (profiling) complete | completed=614, cancelled=0, errors=60"
)


def _sdir(tmp_path: Path, records: bool = True, log: str = DONE) -> Path:
    art = tmp_path / "aiperf"
    (art / "logs").mkdir(parents=True)
    (art / "logs" / "aiperf.log").write_text(log)
    if records:
        shutil.copy(FIXTURE, art / "profile_export.jsonl")
    return tmp_path


def _summary(tmp_path, outcome, **kw):
    s = scenario_from_dict(make_dict())
    return _summarize(s, s.load.stages[0], [], _sdir(tmp_path, **kw), outcome)


OK = Outcome(0, None, False, None)


def test_ok_stage_reads_records_and_drain_line(tmp_path):
    summary = _summary(tmp_path, OK)
    assert summary["status"] == "ok" and summary["records"] == 13
    assert summary["drain_cancelled"] == 0 and summary["user_cancelled"] is None
    assert summary["w_ns"] - summary["t0_ns"] == 5000 * 1_000_000  # warmup 5s
    assert "active_sessions" in summary


def test_stage_json_carries_metrics_verdict_cost_and_cpu_warnings(tmp_path):
    cpu = "\nx - WARNING - AIPerf results may be inaccurate (worker CPU 91%)"
    sdir = _sdir(tmp_path, log=DONE + cpu * 2)
    ok_lines = [line for line in FIXTURE.read_text().splitlines() if '"error"' not in line]
    (sdir / "aiperf" / "profile_export.jsonl").write_text("\n".join(ok_lines))
    load = {"mode": "concurrency", "stages": [{"concurrency": 4, "duration": "20s"}]}
    s = scenario_from_dict(make_dict(load=load))
    summary = _summarize(s, s.load.stages[0], [], sdir, OK)
    assert summary["requests"] == {"success": 12, "error": 0} and summary["cpu_warnings"] == 2
    assert summary["slo"] == {"verdict": "PASS", "violations": []}  # mock: ttft ~200ms, itl 25ms
    assert summary["out_tok_per_s"] > 0 and summary["usd_per_1m_out"] > 0
    assert "2 AIPerf worker CPU warnings: results may be inaccurate" in summary["warnings"]


@pytest.mark.parametrize(
    ("outcome", "records", "expected"),
    [
        (Outcome(0, "startup watchdog: x", False, 1), True, "startup watchdog"),
        (Outcome(2, None, False, None), True, "exit code 2"),
        (OK, False, "no profile_export.jsonl"),
    ],
)
def test_failed_stages_carry_reason_and_log_tail(tmp_path, outcome, records, expected):
    summary = _summary(tmp_path, outcome, records=records, log="line 1\nlast log line")
    assert summary["status"] == "failed"
    assert expected in summary["error"] and "last log line" in summary["error"]


def test_interrupted_stage_is_partial_and_window_ends_at_t_stop(tmp_path):
    stop = 1  # before any record: empty window, no metrics
    summary = _summary(tmp_path, Outcome(0, None, True, stop))
    assert summary["status"] == "partial" and summary["e_eff_ns"] == stop
    assert "active_sessions" not in summary and "ttft_ms" not in summary  # E_eff <= W: all N/A
    assert summary["slo"]["verdict"] == "N/A" and summary["usd_per_1m_out"] is None


def test_interrupted_without_records_is_partial(tmp_path):
    summary = _summary(tmp_path, Outcome(-9, None, True, 5), records=False)
    assert summary["status"] == "partial" and summary["records"] == 0


def test_no_records_is_no_data(tmp_path):
    (tmp_path / "aiperf").mkdir()
    (tmp_path / "aiperf" / "profile_export.jsonl").write_text("")
    s = scenario_from_dict(make_dict())
    summary = _summarize(s, s.load.stages[0], [], tmp_path, OK)
    assert summary["status"] == "no_data" and summary["drain_cancelled"] is None
    assert summary["slo"]["verdict"] == "N/A" and summary["warnings"] == []


def test_arrival_stage_adds_diagnostics(tmp_path):
    s = scenario_from_dict(make_dict(load=ARRIVAL_LOAD))
    summary = _summarize(s, s.load.stages[0], [], _sdir(tmp_path), OK)
    assert summary["status"] == "no_data"  # no plans match the fixture: no arrival t0


def test_masked_api_key_and_exit_codes():
    assert _masked(["--api-key=sk-1", "--model=m"]) == ["--api-key=***", "--model=m"]
    ok, failed = StageResult(1, {"status": "ok"}), StageResult(2, {"status": "failed"})
    assert RunResult(Path("."), (ok,), False).exit_code == 0
    assert RunResult(Path("."), (ok, failed), False).exit_code == 1
    assert RunResult(Path("."), (ok,), True).exit_code == 130


def _scenario_and_personas(stages_count: int = 1):
    load = {"mode": "concurrency", "stages": [{"concurrency": 1, "duration": "1s"}] * stages_count}
    s = scenario_from_dict(make_dict(load=load))
    return s, load_personas(s.personas, s.seed, s.session.language)


def _fake_aiperf(cmd, env, log, console, budget, interrupts, **kw):
    art = log.parent.parent  # <sdir>/aiperf, as AIPerf leaves it after a SIGKILL
    mmap_dir = Path(env["AIPERF_DATASET_MMAP_BASE_PATH"]) / "aiperf_mmap_0123abcd"
    mmap_dir.mkdir(parents=True)
    (mmap_dir / "dataset.dat").write_bytes(b"x" * 1024)
    for name in ("inputs.json", "inputs.tmp"):
        (art / name).write_text("{}")
    return Outcome(-9, None, True, 1)


def test_real_error_run_rate_equals_in_window_error_records_and_fails_slo(tmp_path):
    sdir = _sdir(tmp_path, records=False, log=ERRORS_DONE)
    records = sdir / "aiperf" / "profile_export.jsonl"
    records.write_bytes(gzip.decompress(ERRORS.read_bytes()))
    load = {
        "mode": "concurrency",
        "stages": [{"concurrency": 10, "duration": "3s", "warmup": "1s"}],
    }
    s = scenario_from_dict(make_dict(load=load))
    summary = _summarize(s, s.load.stages[0], [], sdir, OK)
    raw = [json.loads(line) for line in records.read_text().splitlines()]
    window = range(summary["w_ns"], summary["e_eff_ns"])
    started = [r for r in raw if r["metadata"]["request_start_ns"] in window]
    errors = sum("error" in r for r in started)
    assert len(started) >= 300 and summary["error_rate"] == errors / len(started)
    assert 0.05 <= summary["error_rate"] <= 0.15
    assert summary["slo"] == {"verdict": "FAIL", "violations": ["error_rate"]}


def test_drain_cancellations_raise_error_rate_and_cost_follows_out_tokens(tmp_path):
    drained = DONE.replace("cancelled=0", "cancelled=2")
    sdir = _sdir(tmp_path, log=drained)
    ok_lines = [line for line in FIXTURE.read_text().splitlines() if '"error"' not in line]
    (sdir / "aiperf" / "profile_export.jsonl").write_text("\n".join(ok_lines))
    load = {"mode": "concurrency", "stages": [{"concurrency": 4, "duration": "20s"}]}
    s = scenario_from_dict(make_dict(load=load))
    summary = _summarize(s, s.load.stages[0], [], sdir, OK)
    assert summary["drain_cancelled"] == 2 and summary["error_rate"] == pytest.approx(2 / 14)
    usd = 1.09 / (summary["out_tok_per_s"] * 3600) * 1e6
    assert summary["usd_per_1m_out"] == pytest.approx(usd)
    per_1k = 1.09 / (summary["req_per_s"] * 3600) * 1000
    assert summary["usd_per_1k_turns"] == pytest.approx(per_1k)
    assert summary["usd_per_session"] == pytest.approx(per_1k / 1000 * 3.5)  # E[uniform 2..5]


def test_run_stage_removes_inputs_json_temp_file_and_mmap_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(stages, "run_watched", _fake_aiperf)
    s, personas = _scenario_and_personas()
    result = run_stage(s, personas, 1, tmp_path, Interrupts(), cpu_count=4)
    art = tmp_path / "stage_01" / "aiperf"
    assert result.status == "partial" and (art.parent / "stage.json").exists()
    assert not (art / "inputs.json").exists() and not (art / "inputs.tmp").exists()
    assert list(art.glob("aiperf_mmap_*")) == []  # kept out of $TMPDIR, then removed
    dataset = (art.parent / "dataset.jsonl").read_bytes()
    assert result.summary["dataset_sha256"] == hashlib.sha256(dataset).hexdigest()


def test_ctrl_c_during_dataset_generation_never_starts_aiperf(tmp_path, monkeypatch):
    interrupts = Interrupts()
    write = stages.write_dataset

    def write_then_press(*args):
        digest = write(*args)
        interrupts.count += 1
        return digest

    monkeypatch.setattr(stages, "write_dataset", write_then_press)
    monkeypatch.setattr(stages, "run_watched", lambda *a, **k: pytest.fail("AIPerf started"))
    s, personas = _scenario_and_personas()
    assert run_stage(s, personas, 1, tmp_path, interrupts, cpu_count=4) is None


def test_sigterm_stops_like_ctrl_c_and_nohup_sighup_stays_ignored(tmp_path, monkeypatch):
    seen = []

    def fake_stage(scenario, personas, stage_no, run_dir, interrupts, cpu_count):
        seen.append(signal.getsignal(signal.SIGHUP))
        os.kill(os.getpid(), signal.SIGTERM)  # a job runner's kill, mid-stage
        return StageResult(stage_no, {"stage": stage_no, "status": "partial"})

    monkeypatch.setattr(stages, "run_stage", fake_stage)
    before_term = signal.getsignal(signal.SIGTERM)
    before_hup = signal.signal(signal.SIGHUP, signal.SIG_IGN)  # as under nohup
    try:
        s, personas = _scenario_and_personas(stages_count=2)
        result = run_scenario(s, personas, tmp_path)
    finally:
        after_hup = signal.signal(signal.SIGHUP, before_hup)
    assert result.interrupted and result.exit_code == 130 and len(result.stages) == 1
    assert seen == [signal.SIG_IGN] and after_hup is signal.SIG_IGN
    assert signal.getsignal(signal.SIGTERM) is before_term
    report = json.loads((result.run_dir / "report.json").read_text())
    assert [st["stage"] for st in report["stages"]] == [1] and (
        result.run_dir / "report.md"
    ).exists()


def test_taken_prometheus_port_fails_before_any_stage(tmp_path):
    output = {"prometheus_port": 9090}
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen()
        output["prometheus_port"] = held.getsockname()[1]
        s = scenario_from_dict(make_dict(output=output))
        with pytest.raises(RuntimeError, match="prometheus_port"):
            run_scenario(s, [], tmp_path)
    assert list(tmp_path.iterdir()) == []  # no results directory was created
    check_prometheus_port(s.output)  # free again: passes


def test_aiperf_version_check(monkeypatch):
    check_aiperf_version()  # the pinned version is installed
    monkeypatch.setattr(stages, "version", lambda _: "0.13.0")
    with pytest.raises(RuntimeError, match="0.12.0"):
        check_aiperf_version()


def test_check_target_passes_a_listening_server_and_names_a_dead_one():
    from personaload.config import TargetConfig
    from personaload.runner.stages import check_target

    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen()
        port = held.getsockname()[1]
        check_target(TargetConfig(base_url=f"http://127.0.0.1:{port}/v1", model="m"))
    with pytest.raises(
        RuntimeError, match=rf"cannot reach .*127.0.0.1:{port}.*Is the server running"
    ):
        check_target(TargetConfig(base_url=f"http://127.0.0.1:{port}/v1", model="m"), 1.0)


def test_check_target_rejects_an_empty_api_key_env(monkeypatch):
    from personaload.config import TargetConfig
    from personaload.runner.stages import check_target

    monkeypatch.delenv("PL_TEST_KEY", raising=False)
    target = TargetConfig(base_url="http://127.0.0.1:1/v1", model="m", api_key_env="PL_TEST_KEY")
    with pytest.raises(RuntimeError, match="api_key_env names PL_TEST_KEY"):
        check_target(target)


def test_run_dirs_started_in_the_same_second_get_a_suffix(tmp_path, monkeypatch):
    class Frozen:
        @staticmethod
        def now():
            return datetime(2026, 10, 16, 1, 2, 3)

    monkeypatch.setattr(stages, "datetime", Frozen)
    first, second = stages._new_run_dir(tmp_path, "r"), stages._new_run_dir(tmp_path, "r")
    assert (first.name, second.name) == ("r-20261016-010203", "r-20261016-010203-2")


def test_failed_stage_keeps_only_the_log_tail(tmp_path):
    log = tmp_path / "aiperf.log"
    log.write_text("\n".join(f"line {i}" for i in range(100)))
    tail = stages._tail(tmp_path / "missing.log", log).splitlines()
    assert tail == [f"line {i}" for i in range(100 - stages.LOG_TAIL_LINES, 100)]
