"""Scenario stage → `aiperf profile` arguments and environment (SPEC_B §8.2-8.3).

Pure mapping; nothing here starts a process. The runner prepends
`[sys.executable, "-m", "aiperf"]` so AIPerf comes from the same venv.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from personaload.config import ArrivalStage, Scenario, Stage

MAX_WORKERS = 32
REALTIME_METRICS_ENV = "AIPERF_UI_REALTIME_METRICS_ENABLED"
STATS_INTERVAL_S = "5"


def _seconds(ms: float) -> str:
    return format(Decimal(repr(ms / 1000)).normalize(), "f")


def _opt(flag: str, value: object) -> str:
    # --flag=value: cyclopts would read a value starting with "-" (e.g. an API key) as a flag
    return f"{flag}={value}"


def stage_concurrency(stage: Stage) -> int:
    return stage.max_concurrency if isinstance(stage, ArrivalStage) else stage.concurrency


def record_processors(concurrency: int, cpu_count: int) -> int:
    """AIPerf's default formula, passed explicitly: omitting it can stall the run (§8.3)."""
    workers = min(concurrency, max(1, int(cpu_count * 0.75) - 1), MAX_WORKERS)
    return max(1, workers // 4)


def aiperf_args(
    scenario: Scenario,
    stage: Stage,
    dataset_path: Path,
    artifact_dir: Path,
    *,
    cpu_count: int,
    api_key: str | None = None,
) -> list[str]:
    target, load, output = scenario.target, scenario.load, scenario.output
    concurrency = stage_concurrency(stage)
    args = [
        "profile",
        _opt("--model", target.model),
        _opt("--url", target.base_url),
        _opt("--endpoint-type", "chat"),
        "--streaming",
        _opt("--input-file", dataset_path),
        _opt("--custom-dataset-type", "multi_turn"),
        "--use-server-token-count",
        _opt("--tokenizer", target.tokenizer),
        _opt("--record-processors", record_processors(concurrency, cpu_count)),
        _opt("--benchmark-duration", _seconds(stage.warmup + stage.duration)),
        _opt("--benchmark-grace-period", _seconds(load.drain_timeout)),
        _opt("--request-timeout-seconds", _seconds(target.timeout)),
        _opt("--ui", "none"),
        _opt("--artifact-dir", artifact_dir),
    ]
    if target.extra_body:
        body = json.dumps(target.extra_body, separators=(",", ":"), allow_nan=False)
        args.append(_opt("--extra-inputs", body))
    if api_key:
        args.append(_opt("--api-key", api_key))
    if target.max_tokens_field == "max_tokens":
        args.append("--use-legacy-max-tokens")
    if output.prometheus_port is not None:
        args += [
            _opt("--api-port", output.prometheus_port),
            _opt("--api-host", output.prometheus_host),
            _opt("--stats-interval", STATS_INTERVAL_S),
        ]
    if isinstance(stage, ArrivalStage):
        args += ["--fixed-schedule", _opt("--fixed-schedule-start-offset", 0)]
    args.append(_opt("--concurrency", concurrency))
    return args


def aiperf_env(scenario: Scenario) -> dict[str, str]:
    """Environment additions for the AIPerf process."""
    if scenario.output.prometheus_port is None:
        return {}
    return {REALTIME_METRICS_ENV: "true"}
