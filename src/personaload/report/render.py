"""report.json / report.md and the stage table shared with `personaload report` (SPEC_B §9.7).

Only stage.json values are read here, so a results directory without request records renders
the same table.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from personaload.report.judge import headline

SCHEMA_VERSION = 1
NA = "N/A"
ARRIVAL = "arrival_rate"


def _num(value: float | None, spec: str) -> str:
    return NA if value is None else format(value, spec)


def _quantiles(summary: dict, key: str, qs: Sequence[str], spec: str) -> str:
    values = summary.get(key) or {}
    return "/".join(_num(values.get(q), spec) for q in qs)


def _stage_cell(s: dict) -> str:
    status = s.get("status", "ok")
    return f"{s['stage']:02d}" + ("" if status == "ok" else f" ({status})")


def _load_cell(s: dict) -> str:
    config = s.get("config") or {}
    if s.get("mode") != ARRIVAL:
        return str(config.get("concurrency", NA))
    arrival = s.get("arrival") or {}  # absent when the stage has no measurement window
    rate = _num(arrival.get("rate", config.get("sessions_per_sec")), "g")
    cap = _num(arrival.get("max_concurrency", config.get("max_concurrency")), "d")
    return f"{rate} / {_num(arrival.get('actual_rate'), '.2f')} ({cap})"


def _active_cell(s: dict) -> str:
    active = s.get("active_sessions")
    return f"{active['avg']:.1f} ({active['min']}–{active['max']})" if active else NA


def _ttft_cell(s: dict) -> str:
    cell = _quantiles(s, "ttft_ms", ("p50", "p95", "p99"), ".0f")
    censored = s.get("drain_cancelled")
    return f"{cell} ({censored}건 검열(하한값))" if censored else cell


def _cache_cell(s: dict) -> str:
    cache = s.get("cache_hit") or {}
    if not cache.get("eligible"):
        return NA
    rate = cache.get("rate")
    shown = NA if rate is None else f"{rate * 100:.1f}%"
    return f"{shown} (cached 보고 {cache['reported']}/{cache['eligible']}건)"


def _usd(v: float | None, digits: int) -> str:
    return NA if v is None else f"${v:.{digits}f}"


def _row(label: str | None, s: dict, arrival: bool) -> list[str]:
    error_rate = s.get("error_rate")
    usd = s.get("usd_per_1m_out")
    slo = s.get("slo") or {}
    violations = slo.get("violations")
    cells = [
        _stage_cell(s),
        _load_cell(s),
        _active_cell(s),
        _num(s.get("req_per_s"), ".2f"),
        _num(s.get("out_tok_per_s"), ".1f"),
        _ttft_cell(s),
        _quantiles(s, "itl_ms", ("p50", "p95"), ".1f"),
        _num((s.get("e2e_ms") or {}).get("p95"), ".0f"),
        NA if error_rate is None else f"{error_rate * 100:.1f}%",
        _cache_cell(s),
        slo.get("verdict", NA) + (f": {', '.join(violations)}" if violations else ""),
        NA if usd is None else f"${usd:.3f}",
        _usd(s.get("usd_per_1k_turns"), 4),
        _usd(s.get("usd_per_session"), 6),  # a session costs fractions of a cent at real rates
    ]
    if arrival:
        extra = s.get("arrival") or {}
        lag = _quantiles(extra, "start_lag_ms", ("p50", "p95", "p99"), ".0f")
        cells += [lag, str(extra.get("queued", NA)), str(extra.get("not_started", NA))]
    return ([label] if label is not None else []) + cells


def stage_table(rows: Sequence[tuple[str | None, dict]]) -> str:
    """Markdown table; `rows` are (run label or None, stage.json dict)."""
    modes = {s.get("mode") == ARRIVAL for _, s in rows}
    arrival = True in modes
    load = "load" if len(modes) > 1 else "sess/s" if arrival else "conc"
    header = ["stage", load, "active sess", "req/s", "out tok/s", "TTFT p50/p95/p99",
              "ITL p50/p95", "E2E p95", "err%", "cache hit%", "SLO", "$/1M out",
              "$/1K turns", "$/session"]  # fmt: skip
    if arrival:
        header += ["start lag p50/p95/p99", "queued", "not started"]
    if any(label is not None for label, _ in rows):
        header = ["run", *header]
    lines = [header, ["---"] * len(header)]
    lines += [_row(label, s, arrival) for label, s in rows]
    return "\n".join("| " + " | ".join(cells) + " |" for cells in lines)


def headline_text(h: dict) -> str:
    if h.get("stage") is None:
        return h.get("message", NA)
    usd, tps = h.get("usd_per_1m_out"), h.get("out_tok_per_s")
    cost = NA if usd is None else f"${usd:.3f}"
    turns, session = _usd(h.get("usd_per_1k_turns"), 4), _usd(h.get("usd_per_session"), 6)
    per_turn = f", {turns}/1K turns, {session}/session"
    tail = f" (out tok/s {_num(tps, '.1f')}, SLO PASS)"
    return (
        f"stage {h['stage']:02d}: {cost}/1M out"
        + (per_turn if h.get("usd_per_1k_turns") is not None else "")
        + tail
    )


def run_warnings(stages: Sequence[dict]) -> list[str]:
    warnings = []
    for s in stages:
        prefix = f"stage {s['stage']:02d}: "
        if s.get("status") == "failed":
            first = (s.get("error") or "").splitlines()[:1]
            warnings.append(prefix + "failed — " + (first[0] if first else "원인 미상"))
        warnings += [prefix + w for w in s.get("warnings", [])]
    return warnings


def run_report(run_name: str, stages: Sequence[dict]) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "run": run_name,
        "headline": headline(stages),
        "stages": list(stages),
        "warnings": run_warnings(stages),
    }


def render_markdown(report: dict) -> str:
    stages = report["stages"]
    parts = [
        f"# personaload 리포트: {report['run']}",
        f"**헤드라인**: {headline_text(report['headline'])}",
        stage_table([(None, s) for s in stages]) if stages else "실행된 단계 없음",
        "## 경고",
        "\n".join(f"- {w}" for w in report["warnings"]) or "없음",
    ]
    return "\n\n".join(parts) + "\n"


def write_reports(run_dir: Path, stages: Sequence[dict]) -> dict:
    report = run_report(run_dir.name, stages)
    (run_dir / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (run_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")
    return report
