"""personaload command line (SPEC_B §6.3, §8, §9.7, §11)."""

from __future__ import annotations

import argparse
import logging
import os
import shlex
import sys
from importlib.metadata import version
from pathlib import Path

from personaload.config import (
    Scenario,
    ScenarioError,
    dump_scenario,
    format_duration_ms,
    load_scenario,
    parse_duration_ms,
)
from personaload.report.compare import ReportError, compare_markdown
from personaload.report.handoff import handoff_markdown
from personaload.report.judge import headline
from personaload.report.render import headline_text
from personaload.runner.plan import aiperf_args, aiperf_env
from personaload.runner.stages import check_target, resolved_header, run_scenario, stage_dir
from personaload.workload.dataset import write_dataset
from personaload.workload.dists import clamped_mean
from personaload.workload.estimate import MAX_DATASET_BYTES, Checks, check_scenario
from personaload.workload.personas import Persona, PersonaError, load_personas
from personaload.workload.sessions import build_stage_sessions
from personaload.workload.text import CHARS_PER_TOKEN


def _mib(n: int) -> str:
    return f"{n / 2**20:,.1f} MiB"


def _err(line: str) -> None:
    print(line, file=sys.stderr)


def _synthetic_persona_bytes(scenario: Scenario) -> float:
    """Checked before building them: a huge count would otherwise fill memory first."""
    p = scenario.personas
    if p.source != "synthetic":
        return 0.0
    return (
        p.count * clamped_mean(p.system_prompt_tokens) * CHARS_PER_TOKEN[scenario.session.language]
    )


def _prepare(path: str) -> tuple[Scenario, list[Persona], Checks] | None:
    """Load, validate and check a scenario. Prints problems and returns None on errors."""
    try:
        scenario = load_scenario(path)
        if (too_big := _synthetic_persona_bytes(scenario)) > MAX_DATASET_BYTES:
            _err(f"error: personas: {too_big / 2**30:.1f} GiB of synthetic personas > 1 GiB")
            return None
        personas = load_personas(scenario.personas, scenario.seed, scenario.session.language)
    except ScenarioError as exc:
        for line in exc.errors:
            _err(f"error: {line}")
        return None
    except PersonaError as exc:
        _err(f"error: personas.file: {exc}")
        return None
    checks = check_scenario(scenario, personas)
    for line in checks.warnings:
        _err(f"warning: {line}")
    for line in checks.errors:
        _err(f"error: {line}")
    return None if checks.errors else (scenario, personas, checks)


def _format_mean(value: float, is_duration: bool) -> str:
    return format_duration_ms(round(value, 1)) if is_duration else f"{value:.4g}"


def _preview_command(scenario: Scenario, stage_no: int) -> str:
    sdir = stage_dir(Path(scenario.output.dir) / f"{scenario.name}-<timestamp>", stage_no)
    key = (
        "***"
        if scenario.target.api_key_env and os.environ.get(scenario.target.api_key_env)
        else None
    )
    args = aiperf_args(
        scenario,
        scenario.load.stages[stage_no - 1],
        sdir / "dataset.jsonl",
        sdir / "aiperf",
        cpu_count=os.cpu_count() or 1,
        api_key=key,
    )
    env = " ".join(f"{k}={v}" for k, v in aiperf_env(scenario).items())
    return (env + " " if env else "") + shlex.join(["python", "-m", "aiperf", *args])


def cmd_validate(args: argparse.Namespace) -> int:
    prepared = _prepare(args.scenario)
    if prepared is None:
        return 1
    scenario, _, checks = prepared
    print(dump_scenario(scenario, resolved_header(scenario)))
    print("# distribution means (configured -> after clamping)")
    for m in checks.means:
        configured = _format_mean(m.configured, m.is_duration) if m.configured is not None else "-"
        print(f"{m.name}: {configured} → {_format_mean(m.clamped, m.is_duration)}")
    print("\n# per-stage estimates")
    for est in checks.stages:
        print(
            f"stage {est.stage_no:02d}: sessions {est.sessions:,}, "
            f"dataset.jsonl ≈ {_mib(est.dataset_bytes)}, "
            f"AIPerf inputs.json ≈ {_mib(est.inputs_json_bytes)}"
        )
        print(f"  {_preview_command(scenario, est.stage_no)}")
    return 0


def cmd_gen(args: argparse.Namespace) -> int:
    prepared = _prepare(args.scenario)
    if prepared is None:
        return 1
    scenario, personas, _ = prepared
    out = Path(args.out)
    try:
        out.mkdir(parents=True, exist_ok=True)
        (out / "scenario.resolved.yaml").write_text(
            dump_scenario(scenario, resolved_header(scenario)), encoding="utf-8"
        )
        for stage_no in range(1, len(scenario.load.stages) + 1):
            plans = build_stage_sessions(scenario, len(personas), stage_no)
            path = stage_dir(out, stage_no) / "dataset.jsonl"
            digest = write_dataset(path, plans, personas)
            print(f"{path}  sessions {len(plans):,}  sha256 {digest}")
    except OSError as exc:
        _err(f"error: --out {out}: {exc}")
        return 1
    return 0


def _stage_line(summary: dict) -> str:
    parts = [f"stage {summary['stage']:02d}: {summary['status']}"]
    if "records" in summary:
        parts.append(f"requests {summary['records']:,}")
    if active := summary.get("active_sessions"):
        parts.append(
            f"active sessions avg {active['avg']:.2f} (min {active['min']}, max {active['max']})"
        )
    if (tps := summary.get("out_tok_per_s")) is not None:
        parts.append(f"out tok/s {tps:,.1f}")
    if slo := summary.get("slo"):
        parts.append(f"SLO {slo['verdict']}")
    if arrival := summary.get("arrival"):
        parts.append(f"queued {arrival['queued']}, not started {arrival['not_started']}")
    if summary.get("drain_cancelled"):
        parts.append(f"drain cancelled {summary['drain_cancelled']}")
    line = ", ".join(parts)
    return f"{line}\n  {summary['error']}" if summary.get("error") else line


def cmd_run(args: argparse.Namespace) -> int:
    prepared = _prepare(args.scenario)
    if prepared is None:
        return 1
    scenario, personas, _ = prepared
    try:
        check_target(scenario.target)
        result = run_scenario(
            scenario,
            personas,
            Path(args.out or scenario.output.dir),
            progress=lambda line: print(line, flush=True),
        )
    except (RuntimeError, OSError) as exc:
        _err(f"error: {exc}")
        return 1
    for stage in result.stages:
        print(_stage_line(stage.summary))
    if result.interrupted:
        print("interrupted: the remaining stages were skipped")
    print(f"headline: {headline_text(headline([s.summary for s in result.stages]))}")
    print(f"results: {result.run_dir} (report.md)")
    return result.exit_code


def cmd_report(args: argparse.Namespace) -> int:
    dirs = [Path(d) for d in args.dirs]
    try:
        print(compare_markdown(dirs), end="")
        if args.handoff:
            Path(args.handoff).write_text(handoff_markdown(dirs), encoding="utf-8")
            print(f"handoff document: {args.handoff}")
    except (ReportError, OSError) as exc:
        _err(f"error: {exc}")
        return 1
    return 0


def cmd_mockserver(args: argparse.Namespace) -> int:
    from personaload.mockserver.app import MockConfig, parse_listen, serve

    try:
        host, port = parse_listen(args.listen)
        config = MockConfig(
            host=host,
            port=port,
            ttft_ms=parse_duration_ms(args.ttft),
            tps=args.tps,
            jitter=args.jitter,
            error_rate=args.error_rate,
            cache_ttft_factor=args.cache_ttft_factor,
            seed=args.seed,
            usage=not args.no_usage,
        )
    except ValueError as exc:
        for line in str(exc).splitlines():
            _err(f"error: {line}")
        return 1
    try:
        serve(config)
    except OSError as exc:  # bind/resolve failures, e.g. port already in use
        _err(f"error: could not start the mock server on {config.host}:{config.port}: {exc}")
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="personaload",
        description="Load benchmark for OpenAI-compatible LLM servers with chat-shaped traffic.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {version('personaload')}")
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser(
        "validate", help="check a scenario; print it with defaults applied and per-stage estimates"
    )
    validate.add_argument("scenario")
    validate.set_defaults(func=cmd_validate)
    gen = sub.add_parser("gen", help="only generate the per-stage AIPerf multi_turn datasets")
    gen.add_argument("scenario")
    gen.add_argument("--out", required=True, help="output directory (stage_NN/dataset.jsonl)")
    gen.set_defaults(func=cmd_gen)
    run = sub.add_parser(
        "run", help="run AIPerf stage by stage and write stage.json and the report"
    )
    run.add_argument("scenario")
    run.add_argument("--out", help="base results directory (default: output.dir)")
    run.set_defaults(func=cmd_run)
    report = sub.add_parser(
        "report", help="compare results directories from their stage.json files"
    )
    report.add_argument("dirs", nargs="+", help="personaload run results directories")
    report.add_argument(
        "--handoff", help="also write a handoff document (markdown) for a coding agent to this path"
    )
    report.set_defaults(func=cmd_report)
    mock = sub.add_parser(
        "mockserver",
        help="OpenAI-compatible mock server for testing (simulates a prefix cache)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    mock.add_argument("--listen", default="127.0.0.1:8000", help="host:port")
    mock.add_argument("--ttft", default="200ms", help="time to first token (e.g. 200ms)")
    mock.add_argument("--tps", type=float, default=40.0, help="tokens per second per stream")
    mock.add_argument(
        "--jitter", type=float, default=0.0, help="latency multiplier U(1-j, 1+j), 0 <= j < 1"
    )
    mock.add_argument(
        "--error-rate", type=float, default=0.0, help="share of HTTP 500 responses (0-1)"
    )
    mock.add_argument(
        "--cache-ttft-factor", type=float, default=1.0, help="TTFT multiplier on a prefix-cache hit"
    )
    mock.add_argument("--seed", type=int, default=0)
    mock.add_argument(
        "--no-usage",
        action="store_true",
        help="omit the usage field (tests the missing-usage path)",
    )
    mock.set_defaults(func=cmd_mockserver)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s: %(message)s")
    return args.func(args)
