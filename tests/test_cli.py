from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from conftest import ARRIVAL_LOAD, make_dict, write_yaml

from personaload.cli import main
from personaload.config import load_scenario


def test_validate_prints_resolved_scenario_and_estimates(scenario_file, capsys):
    assert main(["validate", str(scenario_file())]) == 0
    out = capsys.readouterr().out
    assert "drain_timeout: 2m" in out
    assert "# distribution means" in out
    assert "session.think_time: 2s →" in out
    assert "stage 01: sessions" in out
    assert "python -m aiperf profile" in out and "--concurrency=4" in out


def test_validate_masks_api_key(scenario_file, capsys, monkeypatch):
    monkeypatch.setenv("CB_TEST_KEY", "sk-secret")
    path = scenario_file(target={"api_key_env": "CB_TEST_KEY"}, output={"prometheus_port": 9090})
    assert main(["validate", str(path)]) == 0
    out = capsys.readouterr().out
    assert "sk-secret" not in out and "'--api-key=***'" in out
    assert "AIPERF_UI_REALTIME_METRICS_ENABLED=true" in out


def test_validate_reports_errors(scenario_file, capsys):
    assert main(["validate", str(scenario_file(slo={"error_rate_max": 2}))]) == 1
    assert "error: slo.error_rate_max" in capsys.readouterr().err


def test_validate_reports_workload_errors_and_warnings(scenario_file, capsys):
    path = scenario_file(
        session={"turns": {"dist": "constant", "value": 1}}, load={"drain_timeout": "10s"}
    )
    assert main(["validate", str(path)]) == 1
    err = capsys.readouterr().err
    assert "warning: load.drain_timeout" in err
    assert "error: load.stages.0" in err


def test_validate_reports_persona_file_errors(scenario_file, capsys, tmp_path):
    path = scenario_file(
        personas={
            "source": "file",
            "file": str(tmp_path / "missing.jsonl"),
            "count": None,
            "system_prompt_tokens": None,
        }
    )
    assert main(["validate", str(path)]) == 1
    assert "personas.file" in capsys.readouterr().err


def test_gen_writes_stage_datasets_and_resolved_scenario(scenario_file, tmp_path, capsys):
    path = scenario_file(
        load={
            "mode": "concurrency",
            "stages": [
                {"concurrency": 2, "duration": "10s", "sessions": 3},
                {"concurrency": 4, "duration": "10s", "sessions": 5},
            ],
        }
    )
    out = tmp_path / "out"
    assert main(["gen", str(path), "--out", str(out)]) == 0
    assert (out / "stage_01" / "dataset.jsonl").read_text().count("\n") == 3
    assert (out / "stage_02" / "dataset.jsonl").read_text().count("\n") == 5
    assert load_scenario(out / "scenario.resolved.yaml") == load_scenario(path)
    assert "sha256" in capsys.readouterr().out


def test_gen_records_persona_file_hash(tmp_path):
    personas = tmp_path / "p.jsonl"
    personas.write_text('{"name": "A", "system_prompt": "You are A."}\n', encoding="utf-8")
    path = write_yaml(
        tmp_path / "s.yaml",
        make_dict(
            personas={
                "source": "file",
                "file": str(personas),
                "count": None,
                "system_prompt_tokens": None,
            }
        ),
    )
    out = tmp_path / "out"
    assert main(["gen", str(path), "--out", str(out)]) == 0
    assert "# personas.file sha256: " in (out / "scenario.resolved.yaml").read_text()


def test_gen_refuses_invalid_scenario(scenario_file, tmp_path):
    assert main(["gen", str(scenario_file(name="")), "--out", str(tmp_path / "o")]) == 1
    assert not (tmp_path / "o").exists()


def _gen_in_subprocess(scenario: Path, out: Path, hash_seed: str) -> None:
    env = {**os.environ, "PYTHONHASHSEED": hash_seed}
    subprocess.run(
        [sys.executable, "-m", "personaload", "gen", str(scenario), "--out", str(out)],
        check=True,
        env=env,
        capture_output=True,
    )


def _tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*.jsonl"))}


def test_gen_is_byte_identical_across_processes_and_hash_seeds(tmp_path):
    data = make_dict(
        load={
            "mode": "concurrency",
            "stages": [
                {"concurrency": 2, "duration": "10s", "sessions": 20},
                {"concurrency": 2, "duration": "10s", "sessions": 20},
            ],
        },
        session={"language": "ko"},
    )
    scenario = write_yaml(tmp_path / "s.yaml", data)
    _gen_in_subprocess(scenario, tmp_path / "a", "1")
    _gen_in_subprocess(scenario, tmp_path / "b", "987654")
    a, b = _tree(tmp_path / "a"), _tree(tmp_path / "b")
    assert len(a) == 2 and a == b

    arrival = write_yaml(tmp_path / "arr.yaml", make_dict(load=ARRIVAL_LOAD))
    _gen_in_subprocess(arrival, tmp_path / "c", "3")
    _gen_in_subprocess(arrival, tmp_path / "d", "4")
    assert _tree(tmp_path / "c") == _tree(tmp_path / "d")

    reseeded = write_yaml(tmp_path / "s2.yaml", {**data, "seed": 43})
    _gen_in_subprocess(reseeded, tmp_path / "e", "1")
    assert _tree(tmp_path / "e") != a


def test_report_compares_directories_and_rejects_empty_ones(tmp_path, capsys):
    stage = {
        "stage": 1,
        "status": "ok",
        "mode": "concurrency",
        "config": {"concurrency": 4},
        "slo": {"verdict": "FAIL", "violations": ["error_rate"]},
        "dataset_sha256": "aa",
    }
    for name in ("a", "b"):
        (tmp_path / name / "stage_01").mkdir(parents=True)
        (tmp_path / name / "stage_01" / "stage.json").write_text(json.dumps(stage))
    assert main(["report", str(tmp_path / "a"), str(tmp_path / "b")]) == 0
    out = capsys.readouterr().out
    assert "- a: no stage passed the SLO" in out and "| b | 01 | 4 |" in out
    (tmp_path / "empty").mkdir()
    assert main(["report", str(tmp_path / "empty")]) == 1
    assert "no stage_*/stage.json" in capsys.readouterr().err


def test_huge_synthetic_persona_pool_is_rejected_before_it_is_built(scenario_file, capsys):
    path = scenario_file(
        personas={"count": 10_000_000, "system_prompt_tokens": {"min": 1000, "max": 1000}}
    )
    assert main(["validate", str(path)]) == 1
    assert "GiB of synthetic personas" in capsys.readouterr().err


def test_gen_into_a_file_is_a_clean_error(scenario_file, tmp_path, capsys):
    blocker = tmp_path / "taken"
    blocker.write_text("x")
    assert main(["gen", str(scenario_file()), "--out", str(blocker)]) == 1
    assert "error: --out" in capsys.readouterr().err
