"""SLO verdict, cost, headline and stage warnings from stage.json values (SPEC_B §9.4-9.6).

Everything here reads plain stage.json dicts, so `personaload report` gets the same headline from
stage.json alone, without the request records.
"""

from __future__ import annotations

from collections.abc import Sequence

from personaload.config import CostConfig, SloConfig

PASS, FAIL, NA = "PASS", "FAIL", "N/A"
NO_PASSING_STAGE = "SLO를 통과한 단계 없음"
LAG_P99_WARN_MS = 100.0


def _p95(summary: dict, key: str) -> float | None:
    return (summary.get(key) or {}).get("p95")


def slo_verdict(summary: dict, slo: SloConfig) -> dict:
    """FAIL if any known value breaks its limit, else N/A if any value is unknown, else PASS."""
    checks = {
        "ttft_p95": (_p95(summary, "ttft_ms"), slo.ttft_p95),
        "itl_p95": (_p95(summary, "itl_ms"), slo.itl_p95),
        "error_rate": (summary.get("error_rate"), slo.error_rate_max),
    }
    violations = [name for name, (v, limit) in checks.items() if v is not None and v > limit]
    if violations:
        return {"verdict": FAIL, "violations": violations}
    unknown = any(v is None for v, _ in checks.values())
    return {"verdict": NA if unknown else PASS, "violations": []}


def usd_per_million_out(out_tok_per_s: float | None, cost: CostConfig) -> float | None:
    if not out_tok_per_s:
        return None
    return cost.gpu_hourly_usd * cost.gpu_count / (out_tok_per_s * 3600) * 1e6


def usd_per_thousand_turns(req_per_s: float | None, cost: CostConfig) -> float | None:
    """GPU cost of 1,000 successful turns (one turn = one request) at the stage's request rate."""
    if not req_per_s:
        return None
    return cost.gpu_hourly_usd * cost.gpu_count / (req_per_s * 3600) * 1000


def headline(stages: Sequence[dict]) -> dict:
    """$/1M out of the fastest complete SLO-passing stage; the lower stage number on ties."""
    candidates = [
        s for s in stages if s.get("status") == "ok" and (s.get("slo") or {}).get("verdict") == PASS
    ]
    if not candidates:
        return {"stage": None, "message": NO_PASSING_STAGE}
    best = max(candidates, key=lambda s: (s.get("out_tok_per_s") or 0, -s["stage"]))
    return {
        "stage": best["stage"],
        "out_tok_per_s": best.get("out_tok_per_s"),
        "usd_per_1m_out": best.get("usd_per_1m_out"),
        "usd_per_1k_turns": best.get("usd_per_1k_turns"),
        "usd_per_session": best.get("usd_per_session"),
    }


def stage_warnings(summary: dict) -> list[str]:
    warnings = []
    if n := summary.get("reused_conversations"):
        warnings.append(
            f"데이터셋 재사용 발생: 대화 {n}개가 두 번 이상 재생됐다 "
            "(단계 sessions나 load.session_pool_factor를 늘린다)"
        )
    if n := (summary.get("active_sessions") or {}).get("unobserved"):
        warnings.append(f"turn 0 레코드가 없는 세션 인스턴스 {n}개는 활성 세션에서 뺐다")
    drain = summary.get("drain_cancelled")
    if drain:
        warnings.append(
            f"drain 뒤 취소 {drain}건: 에러율에 넣었고, 지연 백분위는 검열된 하한값이다"
        )
    elif drain is None and summary.get("status") == "ok":
        warnings.append("drain 취소 미확인: aiperf.log에서 phase 완료 줄을 찾지 못했다")
    if n := summary.get("user_cancelled"):
        warnings.append(f"중단으로 취소된 진행 중 요청 {n}건: 레코드가 없어 집계에서 빠졌다")
    if n := summary.get("usage_missing"):
        warnings.append(f"usage 없음: 청크 수로 대체 ({n}건)")
    lag = summary.get("think_lag_ms") or {}
    if (p99 := lag.get("p99")) is not None and p99 > LAG_P99_WARN_MS:
        excluded = lag.get("excluded_after_error", 0)
        warnings.append(
            f"think lag p99 {p99:.0f}ms > {LAG_P99_WARN_MS:.0f}ms "
            f"(에러 턴 뒤 쌍 {excluded}개 제외): 클라이언트 병목 의심"
        )
    if n := lag.get("below_clock_floor"):
        warnings.append(f"시계 보정 의심: lag가 -5ms보다 작은 턴 쌍 {n}개")
    if n := summary.get("cpu_warnings"):
        warnings.append(f"AIPerf 워커 CPU 경고 {n}회: 결과가 부정확할 수 있다")
    return warnings


def judge_stage(summary: dict, slo: SloConfig, cost: CostConfig, mean_turns: float) -> dict:
    """`mean_turns`: E[turns] of the scenario, so $/session = $/turn x expected turns."""
    per_1k = usd_per_thousand_turns(summary.get("req_per_s"), cost)
    return {
        "slo": slo_verdict(summary, slo),
        "usd_per_1m_out": usd_per_million_out(summary.get("out_tok_per_s"), cost),
        "usd_per_1k_turns": per_1k,
        "usd_per_session": None if per_1k is None else per_1k / 1000 * mean_turns,
        "warnings": stage_warnings(summary),
    }
