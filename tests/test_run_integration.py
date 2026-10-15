"""`personaload run` end to end against the mock server (SPEC_B §8, B3)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import ARRIVAL_LOAD, make_dict, write_yaml
from test_mock_integration import mock_server

pytestmark = pytest.mark.integration


def _scenario(port: int, load: dict, **output) -> dict:
    return make_dict(
        target={"base_url": f"http://127.0.0.1:{port}/v1", "model": "mock-model", "timeout": "20s"},
        session={
            "turns": {"dist": "uniform", "min": 2, "max": 3},
            "think_time": {"dist": "constant", "value": "100ms"},
            "max_output_tokens": {"dist": "uniform", "min": 5, "max": 15},
        },
        load=load,
        output=output,
    )


def _run(scenario_path: Path, out: Path, **kwargs) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "personaload", "run", str(scenario_path), "--out", str(out)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **kwargs,
    )


def _stage(out: Path, n: int) -> tuple[Path, dict]:
    (run_dir,) = out.iterdir()
    sdir = run_dir / f"stage_{n:02d}"
    return sdir, json.loads((sdir / "stage.json").read_text())


def test_run_two_modes_and_cleanup(tmp_path):
    with mock_server("--ttft", "20ms", "--tps", "400") as port:
        load = {
            "mode": "concurrency",
            "stages": [{"concurrency": 4, "duration": "3s", "sessions": 40}],
        }
        conc = write_yaml(tmp_path / "c.yaml", _scenario(port, load, record_requests=False))
        arr = write_yaml(
            tmp_path / "a.yaml",
            _scenario(
                port,
                {
                    **ARRIVAL_LOAD,
                    "stages": [{"sessions_per_sec": 3, "duration": "3s", "max_concurrency": 4}],
                },
            ),
        )
        for path, out in ((conc, tmp_path / "out_c"), (arr, tmp_path / "out_a")):
            proc = _run(path, out)
            stdout, stderr = proc.communicate(timeout=240)
            assert proc.returncode == 0, stdout + stderr

    sdir, stage = _stage(tmp_path / "out_c", 1)
    assert stage["status"] == "ok" and stage["records"] > 0 and stage["mode"] == "concurrency", (
        stage
    )
    assert 3.0 <= stage["active_sessions"]["avg"] <= 4.0, stage
    assert stage["drain_cancelled"] == 0, stage
    assert not (sdir / "aiperf" / "inputs.json").exists()
    assert not (sdir / "aiperf" / "profile_export.jsonl").exists()  # record_requests: false
    assert (sdir / "dataset.jsonl").exists() and (sdir / "aiperf.argv.json").exists()
    assert (sdir.parent / "scenario.resolved.yaml").exists()
    assert stage["slo"]["verdict"] == "PASS" and stage["ttft_ms"]["p50"] > 0, stage
    report = json.loads((sdir.parent / "report.json").read_text())
    assert report["headline"]["stage"] == 1 and (sdir.parent / "report.md").exists()

    sdir, stage = _stage(tmp_path / "out_a", 1)
    assert stage["status"] == "ok" and "arrival" in stage, stage
    assert stage["arrival"]["not_started"] == 0 and stage["arrival"]["rate"] == 3, stage
    assert stage["arrival"]["start_lag_ms"]["p50"] is not None, stage
    assert (sdir / "aiperf" / "profile_export.jsonl").exists()


def test_ctrl_c_saves_partial_stage_and_skips_the_rest(tmp_path):
    with mock_server("--ttft", "20ms", "--tps", "400") as port:
        load = {
            "mode": "concurrency",
            "stages": [
                {"concurrency": 4, "duration": "60s", "sessions": 400},
                {"concurrency": 4, "duration": "60s", "sessions": 400},
            ],
        }
        path = write_yaml(tmp_path / "s.yaml", _scenario(port, load))
        out = tmp_path / "out"
        proc = _run(path, out, start_new_session=True)
        deadline = time.monotonic() + 120
        log = None
        while time.monotonic() < deadline:
            logs = list(out.glob("*/stage_01/aiperf/logs/aiperf.log"))
            if logs and "AIPerf System is PROFILING" in logs[0].read_text():
                log = logs[0]
                break
            time.sleep(0.2)
        assert log is not None, "stage 1 never reached PROFILING"
        time.sleep(3)
        os.kill(proc.pid, signal.SIGINT)  # what a terminal Ctrl+C delivers to personaload
        stdout, stderr = proc.communicate(timeout=90)

    assert proc.returncode == 130, stdout + stderr
    sdir, stage = _stage(out, 1)
    assert stage["status"] == "partial" and stage["records"] > 0
    assert stage["t_stop_ns"] is not None and stage["e_eff_ns"] == stage["t_stop_ns"]
    assert not (sdir.parent / "stage_02").exists()
