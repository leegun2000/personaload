from __future__ import annotations

import pytest
from conftest import ARRIVAL_LOAD, make_dict, write_yaml

from personaload.config import (
    ArrivalStage,
    ConcurrencyStage,
    ScenarioError,
    dump_scenario,
    format_duration_ms,
    load_scenario,
    parse_duration_ms,
    scenario_from_dict,
)


@pytest.mark.parametrize(
    ("text", "ms"),
    [
        ("100ms", 100.0),
        ("30s", 30_000.0),
        ("3m", 180_000.0),
        ("1h", 3_600_000.0),
        ("1.5s", 1500.0),
        ("0s", 0.0),
        ("0.5ms", 0.5),
    ],
)
def test_parse_duration(text, ms):
    assert parse_duration_ms(text) == ms


@pytest.mark.parametrize("bad", ["10", "-1s", "1m30s", "5 s", "1.s", 10, None, "1d"])
def test_parse_duration_rejects(bad):
    with pytest.raises(ValueError):
        parse_duration_ms(bad)


@pytest.mark.parametrize(
    "ms", [0.5, 100.0, 1500.0, 30_000.0, 90_000.0, 3_600_000.0, 8423.1, 1_234_567.0]
)
def test_format_duration_round_trips(ms):
    assert parse_duration_ms(format_duration_ms(ms)) == ms


def test_defaults_applied():
    s = scenario_from_dict(make_dict())
    assert s.target.timeout == 120_000.0
    assert s.target.tokenizer == "builtin"
    assert s.target.max_tokens_field == "max_completion_tokens"
    assert s.load.drain_timeout == s.target.timeout  # D6
    assert s.load.session_pool_factor == 3.0
    assert s.session.language == "en"
    assert s.output.dir == "results/"
    assert s.output.prometheus_port is None
    assert s.output.record_requests is True
    assert s.cost.gpu_count == 1
    assert s.personas.system_prompt_tokens.dist == "uniform"


def test_drain_follows_custom_timeout_and_explicit_drain_wins():
    s = scenario_from_dict(make_dict(target={"timeout": "45s"}))
    assert s.load.drain_timeout == 45_000.0
    s = scenario_from_dict(make_dict(load={"drain_timeout": "10s"}))
    assert s.load.drain_timeout == 10_000.0


def test_stage_type_follows_mode():
    s = scenario_from_dict(make_dict())
    assert isinstance(s.load.stages[0], ConcurrencyStage)
    s = scenario_from_dict(make_dict(load=ARRIVAL_LOAD))
    assert isinstance(s.load.stages[0], ArrivalStage)


def test_dump_round_trips(tmp_path):
    original = scenario_from_dict(make_dict(load=ARRIVAL_LOAD))
    path = tmp_path / "resolved.yaml"
    path.write_text(dump_scenario(original, "# header\n"), encoding="utf-8")
    assert load_scenario(path) == original


def _errors(data) -> str:
    with pytest.raises(ScenarioError) as info:
        scenario_from_dict(data)
    return "\n".join(info.value.errors)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"session": {"turns": {"dist": "uniform", "min": 5, "max": 2}}}, "min must be <= max"),
        (
            {"session": {"turns": {"dist": "uniform", "min": 0, "max": 3}}},
            "turns must be at least 1",
        ),
        (
            {"session": {"user_input_tokens": {"dist": "exponential", "mean": 20}}},
            "user_input_tokens must be at least 1",
        ),
        (
            {"session": {"max_output_tokens": {"dist": "constant", "value": 0}}},
            "max_output_tokens must be at least 1",
        ),
        ({"session": {"turns": {"dist": "uniform", "min": 1.5, "max": 3}}}, "integer"),
        (
            {"session": {"turns": {"dist": "lognormal", "mean": 0, "stddev": 1, "min": 1}}},
            "mean must be > 0",
        ),
        (
            {"session": {"turns": {"dist": "lognormal", "mean": 3, "stddev": -1, "min": 1}}},
            ">= 0",
        ),
        (
            {"session": {"turns": {"dist": "constant", "value": 9, "min": 1, "max": 5}}},
            "within [min, max]",
        ),
        ({"session": {"turns": {"dist": "uniform", "min": 1}}}, "needs max"),
        (
            {"session": {"turns": {"dist": "uniform", "min": 1, "max": 3, "mean": 2}}},
            "does not use mean",
        ),
        (
            {"session": {"think_time": {"dist": "exponential", "mean": "2s"}}},
            "think_time needs max",
        ),
        (
            {"session": {"think_time": {"dist": "exponential", "mean": 2000, "max": "5s"}}},
            "duration",
        ),
        ({"session": {"zipf_s": 0}}, "zipf_s > 0"),
        ({"personas": {"count": 0}}, "greater than or equal to 1"),
        ({"personas": {"count": None}}, "needs count and system_prompt_tokens"),
        (
            {"personas": {"system_prompt_tokens": {"min": 0, "max": 10}}},
            "system_prompt_tokens must be at least 1",
        ),
        (
            {"personas": {"source": "file", "count": None, "system_prompt_tokens": None}},
            "source=file needs file",
        ),
        ({"load": {"stages": [{"concurrency": 0, "duration": "10s"}]}}, "stages.0.concurrency"),
        ({"load": {"stages": [{"concurrency": 1, "duration": "0s"}]}}, "duration must be > 0"),
        (
            {"load": {"stages": [{"concurrency": 1, "duration": "1s", "sessions_per_sec": 2}]}},
            "sessions_per_sec",
        ),
        (
            {
                "load": {
                    **ARRIVAL_LOAD,
                    "stages": [{"sessions_per_sec": 0, "duration": "1s", "max_concurrency": 1}],
                }
            },
            "greater than 0",
        ),
        ({"load": {"stages": []}}, "at least 1"),
        ({"load": {"session_pool_factor": 0.5}}, "session_pool_factor"),
        ({"load": {"drain_timeout": "-1s"}}, "duration"),
        ({"slo": {"error_rate_max": 1.5}}, "error_rate_max"),
        ({"slo": {"ttft_p95": "0s"}}, "must be > 0"),
        ({"cost": {"gpu_hourly_usd": -1}}, "gpu_hourly_usd"),
        ({"cost": {"gpu_count": 0}}, "gpu_count"),
        (
            {"session": {"retrieved_tokens": {"dist": "constant", "value": 0}}},
            "retrieved_tokens must be at least 1",
        ),
        ({"target": {"timeout": "0s"}}, "timeout must be > 0"),
        ({"output": {"prometheus_port": 70000}}, "prometheus_port"),
        ({"name": "bad name/"}, "name"),
        ({"unknown": 1}, "unknown"),
    ],
)
def test_validation_errors(overrides, expected):
    assert expected in _errors(make_dict(**overrides))


def test_constant_think_time_needs_no_max():
    s = scenario_from_dict(make_dict(session={"think_time": {"dist": "constant", "value": "3s"}}))
    assert s.session.think_time.value == 3000.0


def test_load_scenario_reports_io_and_yaml_errors(tmp_path):
    with pytest.raises(ScenarioError, match="cannot read"):
        load_scenario(tmp_path / "missing.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: [unclosed", encoding="utf-8")
    with pytest.raises(ScenarioError, match="YAML"):
        load_scenario(bad)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (
            {"session": {"think_time": {"dist": "exponential", "mean": "0s", "max": "5s"}}},
            "mean must be > 0",
        ),
        ({"slo": {"itl_p95": "0ms"}}, "must be > 0"),
        ({"slo": {"error_rate_max": -0.1}}, "error_rate_max"),
        (
            {
                "load": {
                    **ARRIVAL_LOAD,
                    "stages": [{"sessions_per_sec": 1, "duration": "1s", "max_concurrency": 0}],
                }
            },
            "max_concurrency",
        ),
        ({"load": {"stages": [{"concurrency": 2.5, "duration": "1s"}]}}, "concurrency"),
        ({"load": {"stages": [{"concurrency": True, "duration": "1s"}]}}, "concurrency"),
        ({"load": {"stages": [{"concurrency": "7", "duration": "1s"}]}}, "concurrency"),
        ({"cost": {"gpu_count": True}}, "gpu_count"),
        ({"seed": True}, "seed"),
        ({"output": {"prometheus_port": 0}}, "prometheus_port"),
        (
            {
                "load": {
                    **ARRIVAL_LOAD,
                    "stages": [
                        {
                            "sessions_per_sec": 1,
                            "duration": "1s",
                            "max_concurrency": 2,
                            "concurrency": 3,
                        }
                    ],
                }
            },
            "concurrency",
        ),
        (
            {
                "load": {
                    **ARRIVAL_LOAD,
                    "stages": [
                        {
                            "sessions_per_sec": 1,
                            "duration": "1s",
                            "max_concurrency": 2,
                            "sessions": 3,
                        }
                    ],
                }
            },
            "sessions",
        ),
        ({"session": {"turns": {"dist": "constant", "value": 1.4}}}, "value must be an integer"),
        (
            {"session": {"turns": {"dist": "lognormal", "mean": 1.4, "stddev": 0, "min": 1}}},
            "mean must be an integer",
        ),
        ({"session": {"zipf_s": float("nan")}}, "finite"),
        (
            {
                "session": {
                    "turns": {"dist": "lognormal", "mean": float("nan"), "stddev": 1, "min": 1}
                }
            },
            "finite",
        ),
        ({"load": {"session_pool_factor": float("inf")}}, "finite"),
        (
            {"target": {"extra_body": {"when": __import__("datetime").date(2024, 1, 1)}}},
            "extra_body",
        ),
        ({"target": {"extra_body": {"t": float("nan")}}}, "extra_body"),
    ],
)
def test_more_validation_errors(overrides, expected):
    assert expected in _errors(make_dict(**overrides))


@pytest.mark.parametrize(
    "text", ["2.01s", "16.1s", "8.05s", "32.3s", "0.0000001ms", "1.5h", "0.3m"]
)
def test_decimal_durations_are_exact_and_round_trip(text):
    ms = parse_duration_ms(text)
    assert parse_duration_ms(format_duration_ms(ms)) == ms
    assert parse_duration_ms("2.01s") == 2010.0
    assert parse_duration_ms("16.1s") == 16100.0


def test_resolved_scenario_with_decimal_durations_reloads_identically(tmp_path):
    data = make_dict(
        session={"think_time": {"dist": "constant", "value": "2.01s"}},
        load={"stages": [{"concurrency": 1, "duration": "0.67s", "warmup": "16.1s"}]},
    )
    original = scenario_from_dict(data)
    path = tmp_path / "resolved.yaml"
    path.write_text(dump_scenario(original), encoding="utf-8")
    assert load_scenario(path) == original


def test_extra_body_accepts_nested_json():
    body = {"ignore_eos": True, "chat_template_kwargs": {"enable_thinking": False}, "stop": ["\n"]}
    assert scenario_from_dict(make_dict(target={"extra_body": body})).target.extra_body == body


def test_huge_yaml_integer_is_a_scenario_error(tmp_path):
    path = tmp_path / "s.yaml"
    path.write_text("seed: " + "9" * 5000 + "\n", encoding="utf-8")
    with pytest.raises(ScenarioError, match="YAML"):
        load_scenario(path)


def test_relative_persona_file_is_made_absolute_for_reruns(tmp_path, monkeypatch):
    path = write_yaml(
        tmp_path / "s.yaml", make_dict(personas={"source": "file", "file": "data/p.jsonl"})
    )
    monkeypatch.chdir(tmp_path)
    scenario = load_scenario(path)
    assert scenario.personas.file == str(tmp_path.resolve() / "data" / "p.jsonl")


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"slo": {"error_rate_max": True}}, "error_rate_max"),
        ({"cost": {"gpu_hourly_usd": "2.5"}}, "gpu_hourly_usd"),
        ({"session": {"turns": {"min": True, "max": 4}}}, "turns"),
        (
            {
                "session": {
                    "user_input_tokens": {
                        "dist": "lognormal",
                        "mean": 1,
                        "stddev": 1e200,
                        "min": 1,
                        "max": 9,
                    }
                }
            },
            "too large",
        ),
    ],
)
def test_yaml_booleans_strings_and_overflowing_lognormal_are_rejected(overrides, expected):
    assert expected in _errors(make_dict(**overrides))


def test_non_utf8_scenario_is_a_clean_error(tmp_path):
    path = tmp_path / "s.yaml"
    path.write_bytes(b"name: \xff\xfe\n")
    with pytest.raises(ScenarioError, match="cannot read"):
        load_scenario(path)
