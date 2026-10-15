#!/usr/bin/env python
"""Live smoke: `personaload mockserver` + `personaload gen` + real `aiperf profile` per stage.

Usage (from the repo root, project venv):
  .venv/bin/python .claude/skills/personaload-milestone/scripts/live_smoke.py SCENARIO OUTDIR \
      [--mock "--ttft 200ms --tps 40"] [--stage N] [--raw]

The scenario's target.base_url is pointed at the mock. Prints one JSON summary per stage:
aiperf exit code, hoist/phase log lines, profiling-record TTFT and pooled inter-chunk
percentiles, error count, AIPerf cache %. Exits 1 if any AIPerf run fails.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import socket
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from personaload.config import scenario_from_dict
from personaload.runner.plan import aiperf_args
from ruamel.yaml import YAML

STARTUP_BUDGET_S = 300


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def pct(values: list[float], q: int) -> float | None:
    if len(values) < 2:
        return values[0] if values else None
    return statistics.quantiles(values, n=100, method="inclusive")[q - 1]


def summarize(art: Path) -> dict:
    ttft, icl, errors = [], [], 0
    for line in (art / "profile_export.jsonl").read_text().splitlines():
        rec = json.loads(line)
        if rec["metadata"].get("benchmark_phase") != "profiling":
            continue
        if "error" in rec:
            errors += 1
            continue
        m = rec["metrics"]
        ttft.append(m["time_to_first_token"]["value"])
        icl.extend(m.get("inter_chunk_latency", {}).get("value", []))
    agg = json.loads((art / "profile_export_aiperf.json").read_text())
    log = (art / "logs" / "aiperf.log").read_text()
    grab = lambda key: next((ln.split("] ", 1)[-1] for ln in log.splitlines() if key in ln), None)  # noqa: E731
    return {
        "requests": len(ttft) + errors,
        "errors": errors,
        "ttft_ms": {"p50": pct(ttft, 50), "p95": pct(ttft, 95)},
        "inter_chunk_ms": {"p50": pct(icl, 50), "p95": pct(icl, 95), "n": len(icl)},
        "aiperf_cache_pct": agg.get("overall_usage_prompt_cache_read_pct", {}).get("avg"),
        "hoist": grab("Hoisted leading system turn"),
        "phase": grab("Phase profiling (profiling) complete"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario")
    ap.add_argument("out")
    ap.add_argument("--mock", default="--ttft 200ms --tps 40")
    ap.add_argument("--stage", type=int)
    ap.add_argument("--raw", action="store_true", help="add --export-level=raw (wire payloads)")
    args = ap.parse_args()

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    port = free_port()
    data = YAML(typ="safe").load(Path(args.scenario).read_text(encoding="utf-8"))
    data["target"] = {**data["target"], "base_url": f"http://127.0.0.1:{port}/v1"}
    scenario_path = out / "scenario.yaml"
    YAML().dump(data, scenario_path)
    scenario = scenario_from_dict(data)

    mock = subprocess.Popen(
        [sys.executable, "-m", "personaload", "mockserver", "--listen", f"127.0.0.1:{port}",
         *shlex.split(args.mock)],
        stdout=(out / "mock.log").open("w"), stderr=subprocess.STDOUT, start_new_session=True,
    )  # fmt: skip
    failed = False
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
                break
            except OSError:
                time.sleep(0.2)
        subprocess.run(
            [sys.executable, "-m", "personaload", "gen", str(scenario_path), "--out", str(out / "gen")],
            check=True, capture_output=True,
        )  # fmt: skip
        stages = [args.stage] if args.stage else range(1, len(scenario.load.stages) + 1)
        for n in stages:
            stage = scenario.load.stages[n - 1]
            art = out / f"stage_{n:02d}"
            argv = aiperf_args(scenario, stage, out / "gen" / f"stage_{n:02d}" / "dataset.jsonl",
                               art, cpu_count=os.cpu_count() or 1)  # fmt: skip
            if args.raw:
                argv.append("--export-level=raw")
            budget = (stage.warmup + stage.duration + scenario.load.drain_timeout) / 1000
            proc = subprocess.run([sys.executable, "-m", "aiperf", *argv], capture_output=True,
                                  text=True, timeout=budget + STARTUP_BUDGET_S)  # fmt: skip
            result = {"stage": n, "aiperf_exit": proc.returncode}
            if proc.returncode == 0:
                result |= summarize(art)
            else:
                failed = True
                result["tail"] = (proc.stdout + proc.stderr)[-2000:]
            print(json.dumps(result, ensure_ascii=False))
    finally:
        os.killpg(mock.pid, signal.SIGTERM)
        mock.wait(15)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
