"""personaload command line (SPEC_B §6.3, §8, §9.7, §11)."""

from __future__ import annotations

import argparse
import logging
import os
import shlex
import sys
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
from personaload.runner.stages import resolved_header, run_scenario, stage_dir
from personaload.workload.dataset import write_dataset
from personaload.workload.estimate import Checks, check_scenario
from personaload.workload.personas import Persona, PersonaError, load_personas
from personaload.workload.sessions import build_stage_sessions


def _mib(n: int) -> str:
    return f"{n / 2**20:,.1f} MiB"


def _err(line: str) -> None:
    print(line, file=sys.stderr)


def _prepare(path: str) -> tuple[Scenario, list[Persona], Checks] | None:
    """Load, validate and check a scenario. Prints problems and returns None on errors."""
    try:
        scenario = load_scenario(path)
        personas = load_personas(scenario.personas, scenario.seed, scenario.session.language)
    except ScenarioError as exc:
        for line in exc.errors:
            _err(f"오류: {line}")
        return None
    except PersonaError as exc:
        _err(f"오류: personas.file: {exc}")
        return None
    checks = check_scenario(scenario, personas)
    for line in checks.warnings:
        _err(f"경고: {line}")
    for line in checks.errors:
        _err(f"오류: {line}")
    return None if checks.errors else (scenario, personas, checks)


def _format_mean(value: float, is_duration: bool) -> str:
    return format_duration_ms(round(value, 1)) if is_duration else f"{value:.4g}"


def _preview_command(scenario: Scenario, stage_no: int) -> str:
    sdir = stage_dir(Path(scenario.output.dir) / f"{scenario.name}-<시각>", stage_no)
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
    print("# 분포 평균 (설정 → 클램프 후)")
    for m in checks.means:
        configured = _format_mean(m.configured, m.is_duration) if m.configured is not None else "-"
        print(f"{m.name}: {configured} → {_format_mean(m.clamped, m.is_duration)}")
    print("\n# 단계별 추정")
    for est in checks.stages:
        print(
            f"stage {est.stage_no:02d}: 세션 {est.sessions:,}, "
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
    out.mkdir(parents=True, exist_ok=True)
    (out / "scenario.resolved.yaml").write_text(
        dump_scenario(scenario, resolved_header(scenario)), encoding="utf-8"
    )
    for stage_no in range(1, len(scenario.load.stages) + 1):
        plans = build_stage_sessions(scenario, len(personas), stage_no)
        path = stage_dir(out, stage_no) / "dataset.jsonl"
        digest = write_dataset(path, plans, personas)
        print(f"{path}  세션 {len(plans):,}  sha256 {digest}")
    return 0


def _stage_line(summary: dict) -> str:
    parts = [f"stage {summary['stage']:02d}: {summary['status']}"]
    if "records" in summary:
        parts.append(f"요청 {summary['records']:,}")
    if active := summary.get("active_sessions"):
        parts.append(
            f"활성 세션 평균 {active['avg']:.2f} (min {active['min']}, max {active['max']})"
        )
    if (tps := summary.get("out_tok_per_s")) is not None:
        parts.append(f"out tok/s {tps:,.1f}")
    if slo := summary.get("slo"):
        parts.append(f"SLO {slo['verdict']}")
    if arrival := summary.get("arrival"):
        parts.append(f"대기 {arrival['queued']}, 미시작 {arrival['not_started']}")
    if summary.get("drain_cancelled"):
        parts.append(f"drain 취소 {summary['drain_cancelled']}")
    line = ", ".join(parts)
    return f"{line}\n  {summary['error']}" if summary.get("error") else line


def cmd_run(args: argparse.Namespace) -> int:
    prepared = _prepare(args.scenario)
    if prepared is None:
        return 1
    scenario, personas, _ = prepared
    try:
        result = run_scenario(scenario, personas, Path(args.out or scenario.output.dir))
    except RuntimeError as exc:
        _err(f"오류: {exc}")
        return 1
    for stage in result.stages:
        print(_stage_line(stage.summary))
    if result.interrupted:
        print("중단됨: 남은 단계는 건너뛰었다")
    print(f"헤드라인: {headline_text(headline([s.summary for s in result.stages]))}")
    print(f"결과: {result.run_dir} (report.md)")
    return result.exit_code


def cmd_report(args: argparse.Namespace) -> int:
    dirs = [Path(d) for d in args.dirs]
    try:
        print(compare_markdown(dirs), end="")
        if args.handoff:
            Path(args.handoff).write_text(handoff_markdown(dirs), encoding="utf-8")
            print(f"인계 문서: {args.handoff}")
    except (ReportError, OSError) as exc:
        _err(f"오류: {exc}")
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
            _err(f"오류: {line}")
        return 1
    try:
        serve(config)
    except OSError as exc:  # bind/resolve failures, e.g. port already in use
        _err(f"오류: {config.host}:{config.port}에서 mock 서버를 시작하지 못했다: {exc}")
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="personaload", description=__doc__)
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser(
        "validate", help="시나리오 검증 + 기본값 적용 결과 + 단계별 추정 출력"
    )
    validate.add_argument("scenario")
    validate.set_defaults(func=cmd_validate)
    gen = sub.add_parser("gen", help="단계별 AIPerf multi_turn 데이터셋만 생성")
    gen.add_argument("scenario")
    gen.add_argument("--out", required=True, help="출력 디렉터리 (stage_NN/dataset.jsonl)")
    gen.set_defaults(func=cmd_gen)
    run = sub.add_parser("run", help="단계별로 AIPerf를 실행하고 stage.json을 쓴다")
    run.add_argument("scenario")
    run.add_argument("--out", help="결과 기본 디렉터리 (기본: output.dir)")
    run.set_defaults(func=cmd_run)
    report = sub.add_parser("report", help="결과 디렉터리들의 stage.json으로 비교 표 출력")
    report.add_argument("dirs", nargs="+", help="personaload run 결과 디렉터리")
    report.add_argument("--handoff", help="다른 세션의 코딩 에이전트용 인계 문서(md)를 쓸 경로")
    report.set_defaults(func=cmd_report)
    mock = sub.add_parser("mockserver", help="테스트용 OpenAI 호환 mock 서버 (prefix cache 흉내)")
    mock.add_argument("--listen", default="127.0.0.1:8000", help="host:port")
    mock.add_argument("--ttft", default="200ms", help="첫 토큰 지연 (예: 200ms)")
    mock.add_argument("--tps", type=float, default=40.0, help="스트림당 초당 토큰")
    mock.add_argument("--jitter", type=float, default=0.0, help="지연 배율 U(1-j, 1+j), 0 ≤ j < 1")
    mock.add_argument("--error-rate", type=float, default=0.0, help="HTTP 500 비율 (0~1)")
    mock.add_argument(
        "--cache-ttft-factor", type=float, default=1.0, help="prefix cache 적중 시 TTFT 배율"
    )
    mock.add_argument("--seed", type=int, default=0)
    mock.add_argument("--no-usage", action="store_true", help="usage 필드 생략 (누락 경로 테스트)")
    mock.set_defaults(func=cmd_mockserver)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s: %(message)s")
    return args.func(args)
