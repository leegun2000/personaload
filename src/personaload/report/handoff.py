"""`personaload report <dir>... --handoff FILE`: a self-contained work order for a coding agent in
another session (SPEC_B §9.8). Reads only stage.json and scenario.resolved.yaml.
"""

from __future__ import annotations

import shlex
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ruamel.yaml import YAML, YAMLError

from personaload.report.compare import load_runs, workload_warnings
from personaload.report.judge import FAIL, PASS, headline
from personaload.report.render import NA, headline_text, stage_table

# personaload's own prompts put the shared persona and the history first, so a low hit usually
# points at the server (caching off or evicting) or at a low-reuse scenario, never at how the
# service builds its prompts.
LOW_CACHE_HIT = 0.5
MIN_REQUESTS_FOR_P95 = 20  # below this, p95 is set by the top one or two requests

INTRO = (
    "이 문서는 `personaload report --handoff`가 측정 결과에서 자동으로 만든 작업 지시서다. "
    "다른 세션의 코딩 에이전트가 이 문서만 보고 작업할 수 있게 썼다.\n\n"
    "- personaload는 캐릭터챗 형태의 합성 대화(공유 페르소나, 쌓이는 히스토리)로 OpenAI 호환 "
    "서버에 부하를 걸고, SLO(TTFT·ITL p95, 에러율)를 지키는 최대 처리량 기준으로 출력 토큰 "
    "100만 개당 비용을 잰다.\n"
    "- 프롬프트는 personaload가 만든 것이다. 그래서 아래 할 일은 **서버·엔진·모델 설정**을 "
    "가리킨다. 서비스 자체의 프롬프트 조립은 재지 않았다.\n"
    "- SLO: TTFT p95(첫 토큰까지 시간), ITL p95(스트리밍 청크 간격), 에러율의 상한. "
    "하나라도 넘으면 그 단계는 FAIL이다."
)


@dataclass(frozen=True)
class Rule:
    title: str
    causes: str
    todo: str
    check: str


RULES = {  # tasks are listed in this order
    "failed": Rule(
        "단계 실패",
        "대상 서버가 응답하지 않거나, 단계의 요청이 모두 실패했거나, AIPerf 설정 오류다.",
        "그 단계 디렉터리의 `aiperf/logs/aiperf.log`와 `aiperf.console.log` 끝부분에서 원인을 "
        "찾는다. 서버가 떠 있고 `/v1/chat/completions`에 응답하는지 먼저 확인한다.",
        "같은 시나리오로 다시 재서 그 단계가 ok로 끝나면 완료.",
    ),
    "unjudged": Rule(
        "SLO를 판정할 수 없다 (측정 구간에 끝난 요청이 없거나 중단된 단계)",
        "측정 구간(warmup 뒤 duration) 안에서 시작해 끝난 요청이 없다. 요청 하나가 구간보다 "
        "오래 걸렸거나(서버 포화, target.timeout에 걸렸을 수 있다), 단계가 중단됐다(partial). "
        "응답이 모두 한 청크뿐이면 ITL을 잴 수 없어 N/A가 되기도 한다.",
        "그 단계의 `aiperf/profile_export.jsonl`에서 `request_latency`와 `error`를 확인한다. "
        "포화라면 그 부하에서 서버가 한계에 닿은 것이다. 목표 부하가 그보다 높으면 서버·모델·"
        "장비를 키우고, 아니면 duration을 늘려 다시 잰다. 중단된 단계는 끝까지 다시 잰다.",
        "다시 재서 그 단계가 PASS 또는 FAIL로 판정되면 완료.",
    ),
    "ttft_p95": Rule(
        "첫 토큰이 늦다 (TTFT p95 초과)",
        "입력 처리(prefill) 병목이다. 서버 prefix caching이 꺼져 있거나 잘 안 맞거나, 서버의 "
        "동시 처리 슬롯이 부족하거나, 장비에 비해 모델이 크다.",
        "서버 prefix caching을 켜고(vLLM `--enable-prefix-caching`, llama.cpp는 기본 켜짐) "
        "cache hit를 확인한다. 슬롯·배치 한도를 늘리거나, 더 작은·양자화 모델이나 더 빠른 "
        "장비를 쓴다.",
        "같은 시나리오로 다시 재서 근거의 단계가 SLO PASS(TTFT·ITL·에러율 모두)이고, "
        "헤드라인 $/1M out이 이 문서 값보다 나빠지지 않으면 완료. 지표 하나만 좋아지고 "
        "다른 지표가 나빠지면 완료가 아니다.",
    ),
    "itl_p95": Rule(
        "생성이 느리다 (ITL p95 초과)",
        "출력 생성(decode) 병목이다. 동시에 생성하는 요청이 너무 많거나, 입력 처리가 생성 "
        "단계에 끼어들거나, 모델에 비해 장비 대역폭이 부족하다.",
        "서버의 최대 동시 배치를 낮추거나, prefix caching으로 입력 처리를 줄이거나, 더 작은·"
        "양자화 모델이나 더 빠른 장비를 쓴다.",
        "같은 시나리오로 다시 재서 근거의 단계가 SLO PASS(TTFT·ITL·에러율 모두)이고, "
        "헤드라인 $/1M out이 이 문서 값보다 나빠지지 않으면 완료. 지표 하나만 좋아지고 "
        "다른 지표가 나빠지면 완료가 아니다.",
    ),
    "error_rate": Rule(
        "에러율 초과",
        "코드로 원인을 가른다. 429는 요청 한도, 5xx는 서버 오류·과부하, null은 타임아웃이나 "
        "연결 끊김이다.",
        "서버 로그에서 같은 시간대의 오류를 찾는다. 한도나 타임아웃이면 서버·게이트웨이 "
        "설정을, 과부하면 동시 처리 한도를 조정한다.",
        "같은 시나리오로 다시 재서 근거의 단계가 SLO PASS(TTFT·ITL·에러율 모두)이고, "
        "헤드라인 $/1M out이 이 문서 값보다 나빠지지 않으면 완료. 지표 하나만 좋아지고 "
        "다른 지표가 나빠지면 완료가 아니다.",
    ),
    "cache_unreported": Rule(
        "서버가 cached 토큰을 보고하지 않는다",
        "응답 usage에 `prompt_tokens_details.cached_tokens`가 없다. 캐시 효과를 잴 수 없다.",
        "vLLM은 `--enable-prompt-tokens-details`로 띄운다. 다른 엔진·API는 이 필드를 "
        "지원하는지 확인한다.",
        "다시 재서 cache hit%가 N/A가 아니면 완료.",
    ),
    "cache_low": Rule(
        "prefix cache가 잘 안 맞는다",
        "personaload 워크로드는 보통 캐시가 잘 맞는다(공유 페르소나, 쌓이는 히스토리). "
        f"cache hit가 {LOW_CACHE_HIT:.0%} 아래면 서버 prefix caching이 꺼져 있거나, 캐시가 "
        "밀려나고 있거나, 워크로드 자체의 재사용이 적다(session.turns가 짧고 personas.count가 "
        "세션 수보다 많아 첫 턴 요청이 대부분).",
        "먼저 scenario.resolved.yaml의 session.turns와 personas.count로 워크로드 탓인지 본다. "
        "아니면 서버 prefix caching 설정을 확인하고, KV 캐시 메모리(vLLM "
        "`--gpu-memory-utilization`, llama.cpp `--ctx-size`)를 늘린다.",
        f"다시 재서 cache hit가 {LOW_CACHE_HIT:.0%} 이상이면 완료.",
    ),
    "usage": Rule(
        "서버가 usage를 주지 않는다",
        "스트리밍 응답에 usage가 없어, 출력 토큰을 청크 수로 대신 셌다. 처리량과 비용이 "
        "부정확하다.",
        "서버가 `stream_options.include_usage`를 지원하는지, 버전과 설정을 확인한다.",
        "다시 재서 usage 없음 경고가 사라지면 완료.",
    ),
}


def _num(value: float | None, spec: str) -> str:
    return NA if value is None else format(value, spec)


def _pct(value: float | None) -> str:
    return NA if value is None else f"{value * 100:.1f}%"


def _scenario(run_dir: Path) -> dict:
    """scenario.resolved.yaml as plain data; {} when it is missing or unreadable."""
    try:
        data = YAML(typ="safe").load((run_dir / "scenario.resolved.yaml").read_text("utf-8"))
    except (OSError, YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _stage_evidence(label: str, s: dict, slo: dict) -> list[tuple[str, str]]:
    """(rule key, evidence line) pairs for one stage."""
    at = f"{label} stage {s['stage']:02d}: "
    if s.get("status") == "failed":
        first = (s.get("error") or "").splitlines()[:1]
        return [("failed", at + (first[0] if first else "원인 미상"))]
    found = []
    judged = s.get("slo") or {}
    verdict, violations = judged.get("verdict", NA), judged.get("violations") or []
    requests = s.get("requests") or {}
    if s.get("status") != "ok" or verdict not in (PASS, FAIL):
        n = requests.get("success", 0) + requests.get("error", 0)
        line = f"status {s.get('status', NA)}, SLO {verdict}, 측정 구간 요청 {n}건"
        found.append(("unjudged", at + line + f" (기록 {s.get('records', NA)}건)"))
    ok = requests.get("success")
    sample = f", 성공 요청 {ok}건" if ok is not None else ""
    if "ttft_p95" in violations:
        ttft = _num((s.get("ttft_ms") or {}).get("p95"), ".0f")
        limit = f"기준 {slo.get('ttft_p95', NA)}{sample}"
        found.append(("ttft_p95", at + f"TTFT p95 {ttft} ms ({limit})"))
    if "itl_p95" in violations:
        itl = _num((s.get("itl_ms") or {}).get("p95"), ".1f")
        limit = f"기준 {slo.get('itl_p95', NA)}{sample}"
        found.append(("itl_p95", at + f"ITL p95 {itl} ms ({limit})"))
    if "error_rate" in violations:
        codes = ", ".join(f"{code}×{n}" for code, n in (s.get("error_codes") or {}).items())
        line = f"에러율 {_pct(s.get('error_rate'))} (기준 {_pct(slo.get('error_rate_max'))})"
        found.append(("error_rate", at + line + (f", 코드 {codes}" if codes else "")))
    cache = s.get("cache_hit") or {}
    if cache.get("eligible") and not cache.get("reported"):
        found.append(("cache_unreported", at + f"cached 보고 0/{cache['eligible']}건"))
    elif cache.get("rate") is not None and cache["rate"] < LOW_CACHE_HIT:
        counts = f"cached 보고 {cache['reported']}/{cache['eligible']}건"
        found.append(("cache_low", at + f"cache hit {_pct(cache['rate'])} ({counts})"))
    if n := s.get("usage_missing"):
        found.append(("usage", at + f"usage 없는 요청 {n}건"))
    return found


def _target_line(label: str, sc: dict) -> str:
    target, slo, cost = sc.get("target") or {}, sc.get("slo") or {}, sc.get("cost") or {}
    model = f"`{target['model']}`" if target.get("model") else NA
    url = f"`{target['base_url']}`" if target.get("base_url") else NA
    line = f"- {label}: 모델 {model} @ {url}"
    if slo:
        line += (
            f", SLO TTFT p95 ≤ {slo.get('ttft_p95', NA)}, ITL p95 ≤ {slo.get('itl_p95', NA)}, "
            f"에러율 ≤ {_pct(slo.get('error_rate_max'))}"
        )
    if cost:
        line += f", 원가 기준 시간당 ${cost.get('gpu_hourly_usd', NA)} × {cost.get('gpu_count', 1)}"
        line += f" ({cost['label']})" if cost.get("label") else ""
    return line


def _tasks(evidence: dict[str, list[str]]) -> str:
    blocks = []
    for key, rule in RULES.items():
        if not evidence.get(key):
            continue
        lines = [f"### {len(blocks) + 1}. {rule.title}"]
        lines += [f"- 근거: {e}" for e in evidence[key]]
        lines += [f"- 원인 후보: {rule.causes}", f"- 할 일: {rule.todo}", f"- 확인: {rule.check}"]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) or "없음: 모든 단계가 SLO를 통과했고 고칠 문제가 보이지 않는다."


def _comparison(runs: dict[str, tuple[Path, list[dict]]]) -> list[str]:
    (base_label, (_, base_stages)), *rest = runs.items()
    base_head = headline(base_stages)
    base, base_tps = base_head.get("usd_per_1m_out"), base_head.get("out_tok_per_s")
    lines = []
    for label, (_, stages) in rest:
        head = headline(stages)
        usd, tps = head.get("usd_per_1m_out"), head.get("out_tok_per_s")
        if base == 0 and tps and base_tps:  # local runs priced at $0: compare throughput instead
            delta = (tps / base_tps - 1) * 100
            lines.append(
                f"- {label}: 헤드라인 out tok/s {tps:.1f}, {base_label} 대비 {delta:+.0f}% "
                "(원가 $0이라 처리량으로 비교)"
            )
        elif base and usd is not None:
            delta = (usd / base - 1) * 100
            lines.append(
                f"- {label}: 헤드라인 $/1M out ${usd:.3f}, {base_label} 대비 {delta:+.0f}%"
            )
        else:
            lines.append(
                f"- {label}: {base_label} 대비 비교 불가 (SLO 통과 단계가 없거나 원가 N/A)"
            )
    return lines


def _quality(label: str, stages: list[dict]) -> list[str]:
    """§9.7 stage warnings plus two judgement caveats: tiny samples, and an unsaturated sweep."""
    lines = []
    for s in stages:
        at = f"- {label} stage {s['stage']:02d}: "
        lines += [at + w for w in s.get("warnings", [])]
        n = (s.get("requests") or {}).get("success") or 0
        if 0 < n < MIN_REQUESTS_FOR_P95:
            lines.append(
                at + f"측정 구간의 성공 요청이 {n}건뿐이라 p95가 상위 1~2건으로 정해진다. "
                "판정(PASS 포함)이 우연에 좌우될 수 있으니 duration을 늘려 다시 재서 판단한다."
            )
    top = max(stages, key=lambda s: s["stage"], default=None)
    if top and top.get("status") == "ok" and (top.get("slo") or {}).get("verdict") == PASS:
        lines.append(
            f"- {label} stage {top['stage']:02d}: 최고 부하 단계까지 PASS라 포화점에 닿지 "
            "않았다. 헤드라인은 스윕 상한에서 잘린 값이라 실제 $/1M out은 더 낮을 수 있고, "
            "이 값과의 비교도 그만큼 덜 정확하다. 더 높은 부하 단계를 추가해 다시 잰다."
        )
    return lines


def _rerun(current_dir: Path, has_scenario: bool) -> str:
    """Absolute, quoted commands that work from any directory and any shell PATH."""
    exe = f"{shlex.quote(sys.executable)} -m personaload"
    scenario = shlex.quote(str(current_dir / "scenario.resolved.yaml"))
    run = f"{exe} run {scenario} --out results/after"
    if not has_scenario:
        run = (
            "# scenario.resolved.yaml이 없다: 원래 시나리오 YAML로 실행한다\n"
            f"{exe} run <원래 시나리오.yaml> --out results/after"
        )
    return (
        f"{run}\n{exe} report {shlex.quote(str(current_dir))} results/after/<새 결과 디렉터리> "
        "--handoff handoff.md"
    )


def handoff_markdown(run_dirs: Sequence[Path]) -> str:
    runs = load_runs(run_dirs)
    scenarios = {label: _scenario(path) for label, (path, _) in runs.items()}
    labels = list(runs)
    current = labels[-1]  # earlier runs are references; the work order is about the latest one
    current_dir, current_stages = runs[current]
    evidence: dict[str, list[str]] = {}
    for s in current_stages:
        for key, line in _stage_evidence(current, s, scenarios[current].get("slo") or {}):
            evidence.setdefault(key, []).append(line)
    quality = _quality(current, current_stages)
    many = len(runs) > 1
    results = [f"- {label}: {headline_text(headline(st))}" for label, (_, st) in runs.items()]
    results += _comparison(runs) if many else []
    mismatch = workload_warnings({label: st for label, (_, st) in runs.items()})
    rerun = _rerun(current_dir, bool(scenarios[current]))
    sweep = (
        "\n\n부하를 단계별로 올리는 스윕이라, 헤드라인 단계보다 높은 단계의 FAIL은 서버의 "
        "포화점일 수 있다. 목표 동시 접속 수(또는 도착률)가 정해져 있지 않으면 사용자에게 "
        "먼저 묻는다."
        if evidence and len(current_stages) > 1
        else ""
    )
    raw = [f"- {label}: `{path}`" for label, (path, _) in runs.items()]
    raw.append(
        "\n각 디렉터리: `report.md`(단계 표), `stage_NN/stage.json`(단계 집계), "
        "`stage_NN/aiperf/profile_export.jsonl`(요청별 레코드, `record_requests: false`면 없음), "
        "`stage_NN/aiperf/logs/aiperf.log`(AIPerf 로그)."
    )
    parts = [
        f"# personaload 결과 인계: {', '.join(labels)}",
        INTRO,
        "## 측정 대상",
        "\n".join(_target_line(label, scenarios[label]) for label in labels),
        "## 결과",
        *[f"> 경고: {w}. 이 비교는 공정하지 않다." for w in mismatch],
        "\n".join(results),
        stage_table([(label if many else None, s) for label, (_, st) in runs.items() for s in st]),
        "## 할 일",
        f"마지막 결과({current}) 기준이다.{sweep}",
        _tasks(evidence),
        "## 측정 품질 경고 (서비스 코드 수정 대상 아님)",
        "\n".join(quality) or "없음",
        "## 다시 재는 법",
        "설정을 고친 뒤 같은 시나리오로 다시 잰다. 시나리오가 같으면 데이터셋(sha256)이 같아 "
        "전후 비교가 공정하다.",
        f"```bash\n{rerun}\n```",
        "## 원자료",
        "\n".join(raw),
    ]
    return "\n\n".join(parts) + "\n"
