"""Coding-agent handoff markdown from personaload results (SPEC_B §9.8)."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

from personaload.cli import main
from personaload.report.handoff import handoff_markdown

SCENARIO = """# personaload 시나리오 (기본값 적용 결과)
name: after
target:
  base_url: http://127.0.0.1:8000/v1
  model: local-model
slo:
  ttft_p95: 2s
  itl_p95: 100ms
  error_rate_max: 0.01
cost:
  gpu_hourly_usd: 1.0
  gpu_count: 1
  label: M5
"""


def _stage(n: int, **kw) -> dict:
    return {
        "stage": n,
        "status": "ok",
        "mode": "concurrency",
        "config": {"concurrency": 4},
        "slo": {"verdict": "PASS", "violations": []},
        "out_tok_per_s": 100.0,
        "usd_per_1m_out": 2.778,
        "ttft_ms": {"p50": 200, "p95": 250, "p99": 300},
        "itl_ms": {"p50": 25, "p95": 30},
        "error_rate": 0.0,
        "error_codes": {},
        "cache_hit": {"rate": 0.9, "reported": 10, "eligible": 10},
        "usage_missing": 0,
        "warnings": [],
        "dataset_sha256": "aa",
        **kw,
    }


def _run(root: Path, name: str, stages: list[dict], scenario: str | None = SCENARIO) -> Path:
    run = root / name
    for s in stages:
        (run / f"stage_{s['stage']:02d}").mkdir(parents=True)
        (run / f"stage_{s['stage']:02d}" / "stage.json").write_text(json.dumps(s))
    if scenario is not None:
        (run / "scenario.resolved.yaml").write_text(scenario)
    return run


def _bad_stage() -> dict:
    return _stage(
        2,
        slo={"verdict": "FAIL", "violations": ["ttft_p95", "itl_p95", "error_rate"]},
        ttft_ms={"p95": 3146.9},
        itl_ms={"p95": 181.44},
        error_rate=0.05,
        error_codes={"500": 3, "null": 1},
        cache_hit={"rate": 0.3, "reported": 10, "eligible": 10},
        usage_missing=4,
        warnings=["데이터셋 재사용 발생: 대화 3개가 두 번 이상 재생됐다"],
    )


def test_problems_become_tasks_with_evidence_limits_and_rerun_commands(tmp_path):
    failed = {"stage": 3, "status": "failed", "error": "aiperf 종료 코드 1\nlog tail"}
    run = _run(tmp_path, "after", [_stage(1), _bad_stage(), failed])
    md = handoff_markdown([run])
    where = run.resolve()
    assert md.startswith("# personaload 결과 인계: after\n")
    assert "- after: 모델 `local-model` @ `http://127.0.0.1:8000/v1`" in md
    assert "SLO TTFT p95 ≤ 2s, ITL p95 ≤ 100ms, 에러율 ≤ 1.0%" in md
    titles = [line for line in md.splitlines() if line.startswith("### ")]
    assert titles == [
        "### 1. 단계 실패",
        "### 2. 첫 토큰이 늦다 (TTFT p95 초과)",
        "### 3. 생성이 느리다 (ITL p95 초과)",
        "### 4. 에러율 초과",
        "### 5. prefix cache가 잘 안 맞는다",
        "### 6. 서버가 usage를 주지 않는다",
    ]
    for evidence in (
        "- 근거: after stage 03: aiperf 종료 코드 1",
        "- 근거: after stage 02: TTFT p95 3147 ms (기준 2s)",
        "- 근거: after stage 02: ITL p95 181.4 ms (기준 100ms)",
        "- 근거: after stage 02: 에러율 5.0% (기준 1.0%), 코드 500×3, null×1",
        "- 근거: after stage 02: cache hit 30.0% (cached 보고 10/10건)",
        "- 근거: after stage 02: usage 없는 요청 4건",
    ):
        assert evidence in md, evidence
    assert "- after stage 02: 데이터셋 재사용 발생: 대화 3개가 두 번 이상 재생됐다" in md
    assert f"personaload run {where}/scenario.resolved.yaml --out results/after" in md
    assert f"- after: `{where}`" in md and "`stage_NN/stage.json`(단계 집계)" in md
    assert "log tail" not in md  # only the first line of a failed stage's error


def test_evidence_from_every_stage_goes_under_one_task(tmp_path):
    slow = {"verdict": "FAIL", "violations": ["ttft_p95"]}
    run = _run(tmp_path, "r", [_stage(1, slo=slow, ttft_ms={"p95": 2100}), _stage(2, slo=slow)])
    md = handoff_markdown([run])
    assert md.count("### ") == 1
    assert (
        "- 근거: r stage 01: TTFT p95 2100 ms (기준 2s)\n- 근거: r stage 02: TTFT p95 250 ms" in md
    )
    assert "부하를 단계별로 올리는 스윕이라" in md  # a FAIL above the headline may be saturation


def test_clean_run_has_no_tasks_and_unreported_cache_is_its_own_task(tmp_path):
    clean = handoff_markdown([_run(tmp_path, "clean", [_stage(1)])])
    assert "## 할 일\n\n마지막 결과(clean) 기준이다.\n\n없음: 모든 단계가 SLO를 통과했고" in clean
    assert "최고 부하 단계까지 PASS라 포화점에 닿지 않았다" in clean
    no_usage = _stage(1, cache_hit={"rate": None, "reported": 0, "eligible": 0}, usage_missing=3)
    md = handoff_markdown([_run(tmp_path, "nousage", [no_usage])])
    assert "### 1. 서버가 usage를 주지 않는다" in md and "cached 토큰을 보고하지" not in md
    silent = _stage(1, cache_hit={"rate": None, "reported": 0, "eligible": 12})
    md = handoff_markdown([_run(tmp_path, "silent", [silent])])
    assert "### 1. 서버가 cached 토큰을 보고하지 않는다" in md
    assert "- 근거: silent stage 01: cached 보고 0/12건" in md


def test_comparison_against_the_first_run_and_workload_mismatch(tmp_path):
    before = _run(tmp_path, "before", [_stage(1, usd_per_1m_out=2.0)])
    after = _run(tmp_path, "after", [_stage(1, usd_per_1m_out=1.0, dataset_sha256="bb")])
    md = handoff_markdown([before, after])
    assert "- after: 헤드라인 $/1M out $1.000, before 대비 -50%" in md
    assert "> 경고: 단계 1의 워크로드가 다름. 이 비교는 공정하지 않다." in md


def test_first_run_is_the_base_and_zero_cost_runs_compare_throughput(tmp_path):
    runs = [
        _run(tmp_path, "before", [_stage(1, usd_per_1m_out=2.0)]),
        _run(tmp_path, "mid", [_stage(1, usd_per_1m_out=1.0)]),
        _run(tmp_path, "after", [_stage(1, usd_per_1m_out=4.0)]),
    ]
    md = handoff_markdown(runs)
    assert "- mid: 헤드라인 $/1M out $1.000, before 대비 -50%" in md
    assert "- after: 헤드라인 $/1M out $4.000, before 대비 +100%" in md
    free = [
        _run(tmp_path, "free-a", [_stage(1, usd_per_1m_out=0.0, out_tok_per_s=50.0)]),
        _run(tmp_path, "free-b", [_stage(1, usd_per_1m_out=0.0, out_tok_per_s=75.0)]),
    ]
    md = handoff_markdown(free)
    assert "- free-b: 헤드라인 out tok/s 75.0, free-a 대비 +50% (원가 $0이라 처리량으로 비교)" in md


def test_each_directory_uses_its_own_scenario(tmp_path):
    other = SCENARIO.replace("local-model", "other").replace("ttft_p95: 2s", "ttft_p95: 5s")
    ref = _run(tmp_path, "ref", [_stage(1)], scenario=other)
    slow = {"verdict": "FAIL", "violations": ["ttft_p95"]}
    cur = _run(tmp_path, "cur", [_stage(1, slo=slow, ttft_ms={"p95": 2100})])
    md = handoff_markdown([ref, cur])
    assert "- ref: 모델 `other`" in md and "- cur: 모델 `local-model`" in md
    assert "- 근거: cur stage 01: TTFT p95 2100 ms (기준 2s)" in md


def test_unjudged_stage_is_a_task_not_a_pass(tmp_path):
    stuck = _stage(1, slo={"verdict": "N/A", "violations": []}, records=16)
    stuck |= {"requests": {"success": 0, "error": 0}}
    md = handoff_markdown([_run(tmp_path, "r", [stuck])])
    assert "### 1. SLO를 판정할 수 없다" in md
    assert "- 근거: r stage 01: status ok, SLO N/A, 측정 구간 요청 0건 (기록 16건)" in md
    assert "모든 단계가 SLO를 통과" not in md


def test_cache_threshold_is_strictly_below_half(tmp_path):
    at_half = _stage(1, cache_hit={"rate": 0.5, "reported": 2, "eligible": 2})
    assert "prefix cache" not in handoff_markdown([_run(tmp_path, "half", [at_half])])
    below = _stage(1, cache_hit={"rate": 0.49, "reported": 2, "eligible": 2})
    md = handoff_markdown([_run(tmp_path, "below", [below])])
    assert "### 1. prefix cache가 잘 안 맞는다" in md and "session.turns" in md


def test_small_samples_are_flagged_and_counted(tmp_path):
    slow = {"verdict": "FAIL", "violations": ["ttft_p95"]}
    few = _stage(1, slo=slow, ttft_ms={"p95": 3757.7}) | {"requests": {"success": 13, "error": 0}}
    md = handoff_markdown([_run(tmp_path, "few", [few])])
    assert "TTFT p95 3758 ms (기준 2s, 성공 요청 13건)" in md
    assert "성공 요청이 13건뿐이라 p95가 상위 1~2건으로 정해진다" in md


def test_rerun_commands_are_absolute_and_quoted(tmp_path):
    run = _run(tmp_path, "my run", [_stage(1)])
    md = handoff_markdown([run])
    exe = f"{shlex.quote(sys.executable)} -m personaload"
    assert f"{exe} run {shlex.quote(str(run.resolve() / 'scenario.resolved.yaml'))}" in md
    assert f"{exe} report {shlex.quote(str(run.resolve()))} results/after/" in md


def test_same_run_names_fall_back_to_paths(tmp_path):
    a = _run(tmp_path / "a", "run", [_stage(1, usd_per_1m_out=2.0)])
    b = _run(tmp_path / "b", "run", [_stage(1, usd_per_1m_out=1.0)])
    md = handoff_markdown([a, b])
    assert md.startswith(f"# personaload 결과 인계: {a.resolve()}, {b.resolve()}\n")
    assert f"- {b.resolve()}: 헤드라인 $/1M out $1.000, {a.resolve()} 대비 -50%" in md


def test_tasks_come_from_the_last_run_only(tmp_path):
    broken = _run(tmp_path, "cache-off", [_bad_stage()])
    fixed = _run(tmp_path, "cache-on", [_stage(2)])
    md = handoff_markdown([broken, fixed])
    assert "마지막 결과(cache-on) 기준이다.\n\n없음:" in md
    assert "| cache-off | 02 |" in md  # the reference run still shows in the table
    assert f"personaload run {fixed.resolve()}/scenario.resolved.yaml" in md


def test_missing_scenario_file_still_renders(tmp_path):
    run = _run(tmp_path, "bare", [_bad_stage()], scenario=None)
    md = handoff_markdown([run])
    assert "- 근거: bare stage 02: TTFT p95 3147 ms (기준 N/A)" in md
    assert "- bare: 모델 N/A @ N/A" in md
    assert "# scenario.resolved.yaml이 없다: 원래 시나리오 YAML로 실행한다" in md


def test_cli_writes_the_handoff_file(tmp_path, capsys):
    run = _run(tmp_path, "cli", [_bad_stage()])
    out = tmp_path / "handoff.md"
    assert main(["report", str(run), "--handoff", str(out)]) == 0
    assert out.read_text().startswith("# personaload 결과 인계: cli")
    assert f"인계 문서: {out}" in capsys.readouterr().out
    assert main(["report", str(run), "--handoff", str(tmp_path / "nope" / "h.md")]) == 1
    assert "오류:" in capsys.readouterr().err
