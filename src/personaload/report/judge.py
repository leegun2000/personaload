"""SLO verdict, cost, headline and stage warnings from stage.json values (SPEC_B §9.4-9.6).

Everything here reads plain stage.json dicts, so `personaload report` gets the same headline from
stage.json alone, without the request records.
"""

from __future__ import annotations

from collections.abc import Sequence

from personaload.config import CostConfig, SloConfig

PASS, FAIL, NA = "PASS", "FAIL", "N/A"
NO_PASSING_STAGE = "no stage passed the SLO"
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
            f"dataset reused: {n} conversations were replayed more than once "
            "(raise the stage's sessions or load.session_pool_factor)"
        )
    if n := (summary.get("active_sessions") or {}).get("unobserved"):
        warnings.append(
            f"{n} session instances without a turn-0 record were left out of active sessions"
        )
    drain = summary.get("drain_cancelled")
    if drain:
        warnings.append(
            f"{drain} requests cancelled after the drain: counted as errors; "
            "latency percentiles are censored lower bounds"
        )
    elif drain is None and summary.get("status") == "ok":
        warnings.append("drain cancellations unknown: no phase-complete line in aiperf.log")
    if n := summary.get("user_cancelled"):
        warnings.append(
            f"{n} in-flight requests cancelled by the interrupt: no records, left out of the stats"
        )
    if n := summary.get("usage_missing"):
        warnings.append(f"no usage: output tokens counted from chunks ({n} requests)")
    lag = summary.get("think_lag_ms") or {}
    if (p99 := lag.get("p99")) is not None and p99 > LAG_P99_WARN_MS:
        excluded = lag.get("excluded_after_error", 0)
        warnings.append(
            f"think lag p99 {p99:.0f}ms > {LAG_P99_WARN_MS:.0f}ms "
            f"({excluded} pairs after an error turn excluded): client bottleneck suspected"
        )
    if n := lag.get("below_clock_floor"):
        warnings.append(f"clock skew suspected: {n} turn pairs with lag below -5ms")
    if n := summary.get("cpu_warnings"):
        warnings.append(f"{n} AIPerf worker CPU warnings: results may be inaccurate")
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
