"""Generated datasets and argv against the pinned AIPerf 0.12.0 itself (no server, no benchmark).

These guard the silent AIPerf rules in SPEC_B §7.4/§8.2: MultiTurn schema, system-turn hoist,
fixed-schedule timing detection, and CLI → phase conversion.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ARRIVAL_LOAD, make_dict

from personaload.config import scenario_from_dict
from personaload.runner.plan import aiperf_args
from personaload.workload.dataset import iter_lines, write_dataset
from personaload.workload.personas import load_personas
from personaload.workload.sessions import build_stage_sessions

aiperf = pytest.importorskip("aiperf")
from aiperf.dataset.loader.base_loader import _default_test_run  # noqa: E402
from aiperf.dataset.loader.models import MultiTurn  # noqa: E402
from aiperf.dataset.loader.multi_turn import MultiTurnDatasetLoader  # noqa: E402

pytestmark = pytest.mark.integration


def _build(tmp_path: Path, language: str = "en", load=None):
    s = scenario_from_dict(
        make_dict(session={"language": language}, **({"load": load} if load else {}))
    )
    personas = load_personas(s.personas, s.seed, language)
    plans = build_stage_sessions(s, len(personas), 1)
    path = tmp_path / "dataset.jsonl"
    write_dataset(path, plans, personas)
    return s, plans, path


@pytest.mark.parametrize("language", ["en", "ko"])
@pytest.mark.parametrize("load", [None, ARRIVAL_LOAD])
def test_every_line_validates_as_multiturn(tmp_path, language, load):
    s, plans, path = _build(tmp_path, language, load)
    personas = load_personas(s.personas, s.seed, language)
    lines = list(iter_lines(plans, personas))
    for line in lines:
        model = MultiTurn.model_validate_json(line)
        assert model.turns[0].role == "system"


@pytest.mark.parametrize("load", [None, ARRIVAL_LOAD])
def test_aiperf_loader_hoists_every_persona(tmp_path, load):
    _, plans, path = _build(tmp_path, load=load)
    loader = MultiTurnDatasetLoader(filename=str(path), run=_default_test_run())
    conversations = loader.convert_to_conversations(loader.load_dataset())
    assert [c.session_id for c in conversations] == [p.session_id for p in plans]
    for conv, plan in zip(conversations, plans, strict=True):
        assert conv.system_message.startswith("[Persona P")
        assert len(conv.turns) == len(plan.turns)
        assert [t.max_tokens for t in conv.turns] == [t.output_length for t in plan.turns]
        assert conv.turns[0].timestamp == plan.turns[0].timestamp_ms
        assert [t.delay for t in conv.turns[1:]] == [t.delay_ms for t in plan.turns[1:]]


def _convert(argv):
    """Run AIPerf's CLI parse + config conversion, capturing the plan instead of benchmarking."""
    import aiperf.cli_runner as cli_runner
    from aiperf.cli import app

    captured = []
    original = cli_runner.run_benchmark
    cli_runner.run_benchmark = captured.append
    try:
        app(argv, exit_on_error=False, result_action="return_value")
    finally:
        cli_runner.run_benchmark = original
    assert captured, "AIPerf did not produce a benchmark plan"
    return captured[0].configs[0]


def _resolve(cfg, tmp_path):
    from aiperf.config import BenchmarkRun
    from aiperf.config.resolution.resolvers import DatasetResolver, TimingResolver

    run = BenchmarkRun(benchmark_id="t", cfg=cfg, artifact_dir=tmp_path / "art")
    DatasetResolver().resolve(run)
    TimingResolver().resolve(run)
    return run.resolved


def test_concurrency_argv_converts(tmp_path):
    s, _, path = _build(tmp_path)
    cfg = _convert(aiperf_args(s, s.load.stages[0], path, tmp_path / "art", cpu_count=8))
    phase = next(p for p in cfg.phases if p.name == "profiling")
    assert phase.type == "concurrency"
    assert phase.concurrency == 4
    assert phase.duration == 25
    assert phase.grace_period == 120
    assert cfg.runtime.record_processors == 1
    resolved = _resolve(cfg, tmp_path)
    assert resolved.dataset_session_count == {"main": len(build_stage_sessions(s, 8, 1))}


def test_arrival_argv_converts_and_has_timing(tmp_path):
    s, _, path = _build(tmp_path, load=ARRIVAL_LOAD)
    cfg = _convert(aiperf_args(s, s.load.stages[0], path, tmp_path / "art", cpu_count=8))
    phase = next(p for p in cfg.phases if p.name == "profiling")
    assert phase.type == "fixed_schedule"
    assert phase.concurrency == 16
    assert phase.start_offset == 0
    assert phase.auto_offset is False
    assert _resolve(cfg, tmp_path).dataset_has_timing_data == {"main": True}


def test_dash_leading_values_still_parse(tmp_path):
    s, _, path = _build(tmp_path)
    cfg = _convert(
        aiperf_args(s, s.load.stages[0], path, tmp_path / "art", cpu_count=8, api_key="-sk-dash")
    )
    assert cfg.endpoint.api_key == "-sk-dash"
