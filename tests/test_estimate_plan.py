from __future__ import annotations

import json

import pytest
from conftest import ARRIVAL_LOAD, make_dict

from personaload.config import scenario_from_dict
from personaload.runner.plan import aiperf_args, aiperf_env, record_processors
from personaload.workload.dataset import write_dataset
from personaload.workload.estimate import check_scenario, estimate_stage
from personaload.workload.personas import load_personas
from personaload.workload.sessions import build_stage_sessions


def _scenario(**overrides):
    return scenario_from_dict(make_dict(**overrides))


def _checks(**overrides):
    s = _scenario(**overrides)
    return check_scenario(s, load_personas(s.personas, s.seed, s.session.language))


def test_clean_scenario_has_no_findings():
    checks = _checks()
    assert checks.errors == () and checks.warnings == ()
    assert len(checks.stages) == 1 and checks.stages[0].sessions > 0


def test_warns_on_long_think_time():
    checks = _checks(session={"think_time": {"dist": "uniform", "min": "1s", "max": "90s"}})
    assert any("60s" in w for w in checks.warnings)


def test_warns_when_drain_shorter_than_timeout():
    checks = _checks(load={"drain_timeout": "30s"})
    assert any("drain_timeout" in w for w in checks.warnings)


def test_warns_when_clamping_moves_mean():
    checks = _checks(session={"think_time": {"dist": "exponential", "mean": "10s", "max": "5s"}})
    assert any("session.think_time" in w and "clamped" in w for w in checks.warnings)


def test_error_on_tiny_session_think_time():
    checks = _checks(session={"turns": {"dist": "constant", "value": 1}})
    assert any("L=" in e for e in checks.errors)


def test_error_on_oversized_dataset():
    checks = _checks(load={"stages": [{"concurrency": 1, "duration": "1s", "sessions": 5_000_000}]})
    assert any("1 GiB" in e for e in checks.errors)


def test_tiny_think_time_is_fine_when_sessions_given():
    checks = _checks(
        session={"turns": {"dist": "constant", "value": 1}},
        load={"stages": [{"concurrency": 2, "duration": "10s", "sessions": 5}]},
    )
    assert checks.errors == ()
    assert checks.stages[0].sessions == 5


def test_oversized_arrival_stage_fails_fast_without_materializing(monkeypatch):
    import personaload.workload.estimate as estimate

    def boom(*_):
        raise AssertionError("arrival offsets must not be materialized")

    monkeypatch.setattr(estimate, "stage_session_count", boom)
    stage = {"sessions_per_sec": 1e7, "duration": "1h", "max_concurrency": 1}
    checks = _checks(load={**ARRIVAL_LOAD, "stages": [stage]})
    assert any("GiB" in e for e in checks.errors)


@pytest.mark.parametrize("language", ["en", "ko"])
def test_dataset_estimate_is_close_to_actual(tmp_path, language):
    s = _scenario(
        session={"language": language},
        load={"stages": [{"concurrency": 4, "duration": "20s", "sessions": 400}]},
    )
    personas = load_personas(s.personas, s.seed, language)
    estimate = estimate_stage(s, personas, 1)
    path = tmp_path / "d.jsonl"
    write_dataset(path, build_stage_sessions(s, len(personas), 1), personas)
    actual = path.stat().st_size
    assert estimate.dataset_bytes == pytest.approx(actual, rel=0.2)
    assert estimate.inputs_json_bytes > estimate.dataset_bytes


# --- AIPerf argv -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("concurrency", "cpus", "expected"),
    [(1, 8, 1), (4, 8, 1), (50, 8, 1), (50, 12, 2), (200, 64, 8), (200, 2, 1)],
)
def test_record_processors(concurrency, cpus, expected):
    assert record_processors(concurrency, cpus) == expected


def _flag(args, name):
    """Value of a `--name=value` token (values are always attached, see plan._opt)."""
    matches = [a.split("=", 1)[1] for a in args if a.startswith(name + "=")]
    assert len(matches) == 1, f"{name} appears {len(matches)} times in {args}"
    return matches[0]


def _has(args, name):
    return any(a == name or a.startswith(name + "=") for a in args)


def test_concurrency_argv(tmp_path):
    s = _scenario()
    args = aiperf_args(s, s.load.stages[0], tmp_path / "d.jsonl", tmp_path / "a", cpu_count=8)
    assert args[0] == "profile"
    assert _flag(args, "--concurrency") == "4"
    assert _flag(args, "--benchmark-duration") == "25"
    assert _flag(args, "--benchmark-grace-period") == "120"
    assert _flag(args, "--request-timeout-seconds") == "120"
    assert _flag(args, "--custom-dataset-type") == "multi_turn"
    assert _flag(args, "--tokenizer") == "builtin"
    assert _flag(args, "--record-processors") == "1"
    for flag in ("--streaming", "--use-server-token-count"):
        assert flag in args
    for absent in (
        "--fixed-schedule",
        "--request-count",
        "--api-port",
        "--api-key",
        "--extra-inputs",
        "--use-legacy-max-tokens",
    ):
        assert not _has(args, absent)
    # every valued option is attached with "=", so no bare value can be misread as a flag
    assert all(a.startswith("--") or a == "profile" for a in args)
    assert aiperf_env(s) == {}


def test_arrival_argv_and_options(tmp_path):
    s = _scenario(
        load=ARRIVAL_LOAD,
        target={
            "extra_body": {"ignore_eos": True},
            "max_tokens_field": "max_tokens",
            "timeout": "1.5s",
        },
        output={"prometheus_port": 9090},
    )
    args = aiperf_args(
        s, s.load.stages[0], tmp_path / "d.jsonl", tmp_path / "a", cpu_count=8, api_key="sk-x"
    )
    assert _flag(args, "--fixed-schedule-start-offset") == "0"
    assert "--fixed-schedule" in args
    assert _flag(args, "--concurrency") == "16"
    assert "--request-count" not in args
    assert json.loads(_flag(args, "--extra-inputs")) == {"ignore_eos": True}
    assert _flag(args, "--api-key") == "sk-x"
    assert "--use-legacy-max-tokens" in args
    assert _flag(args, "--request-timeout-seconds") == "1.5"
    assert _flag(args, "--api-port") == "9090"
    assert _flag(args, "--api-host") == "127.0.0.1"
    assert aiperf_env(s) == {"AIPERF_UI_REALTIME_METRICS_ENABLED": "true"}


@pytest.mark.parametrize(
    ("timeout", "seconds"), [("16.1s", "16.1"), ("0.5ms", "0.0005"), ("2m", "120"), ("90s", "90")]
)
def test_seconds_are_exact(tmp_path, timeout, seconds):
    s = _scenario(target={"timeout": timeout})
    args = aiperf_args(s, s.load.stages[0], tmp_path / "d", tmp_path / "a", cpu_count=8)
    assert _flag(args, "--request-timeout-seconds") == seconds
