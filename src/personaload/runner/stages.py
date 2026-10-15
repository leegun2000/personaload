"""Stage orchestration: results directory, dataset, AIPerf run and stage.json (SPEC_B §8)."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sys
from dataclasses import dataclass
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from personaload.config import ArrivalStage, OutputConfig, Scenario, Stage, dump_scenario
from personaload.report.aggregate import (
    active_sessions,
    arrival_diagnostics,
    measurement_window,
    request_metrics,
    reused_conversations,
    think_lag,
)
from personaload.report.judge import judge_stage
from personaload.report.records import Record, load_records, parse_cancellations
from personaload.report.render import SCHEMA_VERSION, write_reports
from personaload.runner.plan import aiperf_args, aiperf_env
from personaload.runner.process import RUN_SLACK_S, Interrupts, Outcome, run_watched
from personaload.workload.dataset import write_dataset
from personaload.workload.dists import clamped_mean
from personaload.workload.personas import Persona, file_sha256
from personaload.workload.sessions import SessionPlan, build_stage_sessions

AIPERF_VERSION = "0.12.0"
LOG_TAIL_LINES = 30
CPU_WARNING = "AIPerf results may be inaccurate"  # worker CPU above 85% (§9.6)
# AIPerf writes a dataset-sized aiperf_mmap_<id>/ under $TMPDIR and leaves it behind (empty after
# a normal exit, full after SIGKILL). Keep it in the stage directory, where run_stage removes it.
MMAP_BASE_ENV = "AIPERF_DATASET_MMAP_BASE_PATH"
# a terminal Ctrl+C, a job-runner kill and a closed terminal all stop AIPerf the same way (§8.5)
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def stage_dir(root: Path, stage_no: int) -> Path:
    return root / f"stage_{stage_no:02d}"


def resolved_header(scenario: Scenario) -> str:
    header = "# personaload 시나리오 (기본값 적용 결과)\n"
    if scenario.personas.source == "file":
        header += f"# personas.file sha256: {file_sha256(scenario.personas.file)}\n"
    return header


def check_aiperf_version() -> None:
    try:
        found = version("aiperf")
    except PackageNotFoundError:
        found = None
    if found != AIPERF_VERSION:
        raise RuntimeError(f"aiperf=={AIPERF_VERSION}이 필요하다 (설치된 버전: {found})")


async def _probe_listen(host: str, port: int) -> None:
    server = await asyncio.get_running_loop().create_server(asyncio.Protocol, host, port)
    server.close()  # same bind uvicorn does (all resolved addresses, SO_REUSEADDR)
    await server.wait_closed()


def check_prometheus_port(output: OutputConfig) -> None:
    """A taken API port kills only AIPerf's API service; the rest waits for the 600 s startup
    watchdog with nothing in the log naming the port. Fail before the first stage instead."""
    if output.prometheus_port is None:
        return
    try:
        asyncio.run(_probe_listen(output.prometheus_host, output.prometheus_port))
    except OSError as exc:
        where = f"{output.prometheus_host}:{output.prometheus_port}"
        raise RuntimeError(
            f"output.prometheus_port {where}을 열 수 없다: {exc} "
            "(Prometheus 서버 등 다른 프로세스가 쓰는지 확인)"
        ) from exc


@dataclass(frozen=True)
class StageResult:
    stage_no: int
    summary: dict

    @property
    def status(self) -> str:
        return self.summary["status"]


@dataclass(frozen=True)
class RunResult:
    run_dir: Path
    stages: tuple[StageResult, ...]
    interrupted: bool

    @property
    def exit_code(self) -> int:
        if self.interrupted:
            return 130
        return 1 if any(s.status == "failed" for s in self.stages) else 0


def _masked(args: list[str]) -> list[str]:
    return ["--api-key=***" if a.startswith("--api-key=") else a for a in args]


def _tail(*paths: Path) -> str:
    for path in paths:
        if path.exists():
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(lines[-LOG_TAIL_LINES:])
    return ""


def _failure(outcome: Outcome, records: Path) -> str | None:
    if outcome.watchdog:
        return outcome.watchdog
    if outcome.interrupted:
        return None
    if outcome.exit_code != 0:
        return f"aiperf 종료 코드 {outcome.exit_code}"
    if not records.exists():
        return "profile_export.jsonl이 없다"
    return None


def _measure(
    stage: Stage,
    plans: list[SessionPlan],
    records: list[Record],
    t_stop_ns: int | None,
    drain_cancelled: int | None,
) -> dict:
    """Window and metrics (§9.2-9.3, §9.6). Empty when no record places the window; only the
    window bounds when E_eff <= W, since every metric is then N/A."""
    arrival = isinstance(stage, ArrivalStage)
    by_id = {p.session_id: p for p in plans}
    window = measurement_window(records, by_id, stage.warmup, stage.duration, t_stop_ns, arrival)
    if window is None:
        return {}
    measured = {"t0_ns": window.t0, "w_ns": window.w, "e_eff_ns": window.e_eff}
    if window.empty:
        return measured
    active = active_sessions(records, by_id, window)
    measured |= request_metrics(records, window, drain_cancelled) | {
        "active_sessions": {
            "avg": active.avg, "min": active.min, "max": active.max, "unobserved": active.unobserved
        },
        "think_lag_ms": think_lag(records, by_id, window),
    }  # fmt: skip
    if arrival:
        diag = arrival_diagnostics(records, by_id, window, stage.warmup, stage.duration)
        measured["arrival"] = diag | {
            "rate": stage.sessions_per_sec, "max_concurrency": stage.max_concurrency
        }  # fmt: skip
    return measured


def _summarize(
    scenario: Scenario,
    stage: Stage,
    plans: list[SessionPlan],
    sdir: Path,
    outcome: Outcome,
) -> dict:
    artifacts = sdir / "aiperf"
    records_path = artifacts / "profile_export.jsonl"
    log_path = artifacts / "logs" / "aiperf.log"
    summary: dict = {"aiperf_exit_code": outcome.exit_code, "t_stop_ns": outcome.t_stop_ns}
    failure = _failure(outcome, records_path)
    if failure:
        tail = _tail(log_path, sdir / "aiperf.console.log")
        return summary | {"status": "failed", "error": f"{failure}\n{tail}".strip()}
    records = load_records(records_path) if records_path.exists() else []
    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    cancel = parse_cancellations(log_text)
    measured = _measure(stage, plans, records, outcome.t_stop_ns, cancel.drain)
    status = "partial" if outcome.interrupted else "ok" if measured else "no_data"
    summary |= {
        "status": status,
        "records": len(records),
        "drain_cancelled": cancel.drain,
        "user_cancelled": cancel.user,
        "cpu_warnings": log_text.count(CPU_WARNING),
        "reused_conversations": reused_conversations(records),
        **measured,
    }
    mean_turns = clamped_mean(scenario.session.turns)
    return summary | judge_stage(summary, scenario.slo, scenario.cost, mean_turns)


def run_stage(
    scenario: Scenario,
    personas: list[Persona],
    stage_no: int,
    run_dir: Path,
    interrupts: Interrupts,
    cpu_count: int,
) -> StageResult | None:
    """None when Ctrl+C arrived while the dataset was being written: the stage never starts."""
    stage = scenario.load.stages[stage_no - 1]
    sdir = stage_dir(run_dir, stage_no)
    plans = build_stage_sessions(scenario, len(personas), stage_no)
    digest = write_dataset(sdir / "dataset.jsonl", plans, personas)
    if interrupts.count:
        return None
    key_env = scenario.target.api_key_env
    api_key = os.environ.get(key_env) if key_env else None
    args = aiperf_args(
        scenario,
        stage,
        sdir / "dataset.jsonl",
        sdir / "aiperf",
        cpu_count=cpu_count,
        api_key=api_key,
    )
    extra_env = aiperf_env(scenario)
    (sdir / "aiperf.argv.json").write_text(
        json.dumps(
            {"argv": ["python", "-m", "aiperf", *_masked(args)], "env": extra_env}, indent=2
        ),
        encoding="utf-8",
    )
    budget_s = (stage.warmup + stage.duration + scenario.load.drain_timeout) / 1000 + RUN_SLACK_S
    outcome = run_watched(
        [sys.executable, "-m", "aiperf", *args],
        {**os.environ, **extra_env, MMAP_BASE_ENV: str(sdir / "aiperf")},
        sdir / "aiperf" / "logs" / "aiperf.log",
        sdir / "aiperf.console.log",
        budget_s,
        interrupts,
    )
    summary = {
        "schema_version": SCHEMA_VERSION,
        "stage": stage_no,
        "mode": scenario.load.mode,
        "config": stage.model_dump(mode="json"),
        "dataset_sha256": digest,
        "sessions_in_dataset": len(plans),
    } | _summarize(scenario, stage, plans, sdir, outcome)
    # inputs.json repeats the persona per turn (≈ dataset × E[T]); dataset.jsonl regenerates it.
    # inputs.tmp is its half-written form, left behind when AIPerf is SIGKILLed.
    for name in ("inputs.json", "inputs.tmp"):
        (sdir / "aiperf" / name).unlink(missing_ok=True)
    for mmap_dir in (sdir / "aiperf").glob("aiperf_mmap_*"):
        shutil.rmtree(mmap_dir, ignore_errors=True)
    if not scenario.output.record_requests:
        for name in ("profile_export.jsonl", "profile_export_raw.jsonl"):
            (sdir / "aiperf" / name).unlink(missing_ok=True)
    (sdir / "stage.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), "utf-8")
    return StageResult(stage_no, summary)


def _new_run_dir(base: Path, name: str) -> Path:
    stem = base / f"{name}-{datetime.now():%Y%m%d-%H%M%S}"
    path, n = stem, 1
    while path.exists():
        n += 1
        path = stem.with_name(f"{stem.name}-{n}")
    path.mkdir(parents=True)
    return path


def run_scenario(
    scenario: Scenario, personas: list[Persona], base_dir: Path, cpu_count: int | None = None
) -> RunResult:
    """Run every stage in order. Stops at the first failed stage, or on Ctrl+C, SIGTERM or
    SIGHUP (each forwarded to AIPerf as one SIGINT)."""
    check_aiperf_version()
    check_prometheus_port(scenario.output)
    run_dir = _new_run_dir(base_dir, scenario.name)
    (run_dir / "scenario.resolved.yaml").write_text(
        dump_scenario(scenario, resolved_header(scenario)), encoding="utf-8"
    )
    interrupts = Interrupts()
    previous = {sig: signal.getsignal(sig) for sig in STOP_SIGNALS}
    for sig, handler in previous.items():
        if handler is not signal.SIG_IGN:  # keep nohup's ignored SIGHUP
            signal.signal(sig, interrupts.handle)
    results: list[StageResult] = []
    try:
        for stage_no in range(1, len(scenario.load.stages) + 1):
            if interrupts.count:
                break
            result = run_stage(
                scenario, personas, stage_no, run_dir, interrupts, cpu_count or os.cpu_count() or 1
            )
            if result is None:
                break
            results.append(result)
            if result.status == "failed":
                break
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    write_reports(run_dir, [r.summary for r in results])
    return RunResult(run_dir, tuple(results), interrupts.count > 0)
