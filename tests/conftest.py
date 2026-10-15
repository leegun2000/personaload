from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

BASE: dict[str, Any] = {
    "name": "unit",
    "seed": 42,
    "target": {"base_url": "http://localhost:8000/v1", "model": "mock-model"},
    "personas": {"count": 8, "system_prompt_tokens": {"min": 200, "max": 400}},
    "session": {
        "turns": {"dist": "uniform", "min": 2, "max": 5},
        "user_input_tokens": {"dist": "lognormal", "mean": 30, "stddev": 15, "min": 5, "max": 120},
        "max_output_tokens": {"dist": "uniform", "min": 20, "max": 60},
        "think_time": {"dist": "exponential", "mean": "2s", "min": "100ms", "max": "10s"},
        "persona_selection": "zipf",
        "zipf_s": 1.1,
    },
    "load": {
        "mode": "concurrency",
        "stages": [{"concurrency": 4, "duration": "20s", "warmup": "5s"}],
    },
    "slo": {"ttft_p95": "1s", "itl_p95": "100ms", "error_rate_max": 0.01},
    "cost": {"gpu_hourly_usd": 1.09},
}

ARRIVAL_LOAD: dict[str, Any] = {
    "mode": "arrival_rate",
    "stages": [{"sessions_per_sec": 2, "duration": "10s", "warmup": "2s", "max_concurrency": 16}],
}


def make_dict(**overrides: Any) -> dict[str, Any]:
    """Deep copy of BASE with top-level sections replaced or merged (dicts merge one level)."""
    data = copy.deepcopy(BASE)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key] = {**data[key], **copy.deepcopy(value)}
        else:
            data[key] = copy.deepcopy(value)
    return data


def write_yaml(path: Path, data: dict[str, Any]) -> Path:
    yaml = YAML()
    with path.open("w", encoding="utf-8") as fh:
        yaml.dump(data, fh)
    return path


@pytest.fixture
def scenario_file(tmp_path: Path):
    def _make(**overrides: Any) -> Path:
        return write_yaml(tmp_path / "scenario.yaml", make_dict(**overrides))

    return _make
