"""`personaload mockserver` as a real process: restart determinism and a real AIPerf run (B2)."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest
from conftest import make_dict, write_yaml

from personaload.config import scenario_from_dict
from personaload.runner.plan import aiperf_args

pytestmark = pytest.mark.integration


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def mock_server(*flags: str, hash_seed: str = "0", log: Path | None = None):
    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "personaload",
            "mockserver",
            "--listen",
            f"127.0.0.1:{port}",
            *flags,
        ],
        env={**os.environ, "PYTHONHASHSEED": hash_seed},
        stdout=subprocess.DEVNULL,
        stderr=log.open("w") if log else subprocess.PIPE,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
                break
            except OSError:
                if proc.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("mock server did not start") from None
                time.sleep(0.1)
        yield port
    finally:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(10)


def _post(port: int, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def _outcomes(port: int) -> list[tuple[int, int | None]]:
    out = []
    for i in range(12):
        body = {
            "model": "m",
            "max_tokens": 50,
            "messages": [{"role": "user", "content": f"question {i}"}],
        }
        status, payload = _post(port, body)
        out.append((status, payload.get("usage", {}).get("completion_tokens")))
    return out


def test_cli_rejects_bad_arguments():
    proc = subprocess.run(
        [sys.executable, "-m", "personaload", "mockserver", "--error-rate", "30"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 1 and "비율(0~1)" in proc.stderr


def test_same_seed_same_outcomes_after_restart():
    flags = ("--ttft", "0ms", "--tps", "10000", "--error-rate", "0.4", "--seed", "11")
    with mock_server(*flags, hash_seed="1") as port:
        first = _outcomes(port)
    with mock_server(*flags, hash_seed="424242") as port:
        second = _outcomes(port)
    assert first == second
    statuses = {s for s, _ in first}
    assert statuses == {200, 500}  # both paths exercised


def test_real_aiperf_run_against_mock(tmp_path: Path):
    with mock_server("--ttft", "20ms", "--tps", "400", "--cache-ttft-factor", "0.5") as port:
        data = make_dict(
            target={
                "base_url": f"http://127.0.0.1:{port}/v1",
                "model": "mock-model",
                "timeout": "30s",
            },
            session={
                "turns": {"dist": "uniform", "min": 2, "max": 3},
                "think_time": {"dist": "constant", "value": "100ms"},
                "max_output_tokens": {"dist": "uniform", "min": 5, "max": 15},
            },
            load={
                "mode": "concurrency",
                "stages": [{"concurrency": 2, "duration": "4s", "sessions": 4}],
            },
        )
        scenario_path = write_yaml(tmp_path / "s.yaml", data)
        out = tmp_path / "gen"
        subprocess.run(
            [sys.executable, "-m", "personaload", "gen", str(scenario_path), "--out", str(out)],
            check=True,
            capture_output=True,
            timeout=60,
        )
        s = scenario_from_dict(data)
        art = tmp_path / "art"
        args = aiperf_args(
            s,
            s.load.stages[0],
            out / "stage_01" / "dataset.jsonl",
            art,
            cpu_count=os.cpu_count() or 1,
        )
        proc = subprocess.run(
            [sys.executable, "-m", "aiperf", *args], capture_output=True, text=True, timeout=180
        )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    summary = json.loads((art / "profile_export_aiperf.json").read_text())
    assert summary["request_count"]["avg"] > 0
    assert "error_request_count" not in summary  # omitted by AIPerf when there are no errors
    assert summary["total_usage_prompt_tokens"]["avg"] > 0  # server usage was read
    assert summary["overall_usage_prompt_cache_read_pct"]["avg"] > 0  # cached_tokens was read
    log = (art / "logs" / "aiperf.log").read_text()
    assert "Hoisted leading system turn into system_message for 4/4" in log


def _abort(port: int, body: dict) -> None:
    """Send a request and drop the connection after 0.2 s (mid-TTFT)."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with (
        pytest.raises((TimeoutError, urllib.error.URLError)),
        urllib.request.urlopen(req, timeout=0.2) as resp,  # streams return headers at once
    ):
        resp.read()


def test_aborted_requests_register_nothing_and_log_no_tracebacks(tmp_path: Path):
    log = tmp_path / "mock.log"
    with mock_server("--ttft", "600ms", "--tps", "1000", log=log) as port:
        for stream in (False, True):
            messages = [{"role": "user", "content": f"abort stream={stream}"}]
            _abort(port, {"model": "m", "stream": stream, "messages": messages})
            time.sleep(1.0)
            _, body = _post(port, {"model": "m", "messages": messages})
            assert body["usage"]["prompt_tokens_details"]["cached_tokens"] == 0
    assert "Error handling request" not in log.read_text()


def test_sigterm_does_not_wait_for_inflight_streams():
    import threading

    with mock_server("--ttft", "30s") as port:
        body = {"model": "m", "stream": True, "messages": [{"role": "user", "content": "slow"}]}
        worker = threading.Thread(target=lambda: _post_quietly(port, body), daemon=True)
        worker.start()
        time.sleep(0.5)
        started = time.monotonic()
    # the context manager sent SIGTERM and waited for exit
    assert time.monotonic() - started < 5


def _post_quietly(port: int, body: dict) -> None:
    with contextlib.suppress(Exception):  # the server drops this connection on SIGTERM
        _post(port, body)


def test_port_in_use_is_a_clean_error():
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        port = holder.getsockname()[1]
        proc = subprocess.run(
            [sys.executable, "-m", "personaload", "mockserver", "--listen", f"127.0.0.1:{port}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    assert proc.returncode == 1
    assert "시작하지 못했다" in proc.stderr and "Traceback" not in proc.stderr
