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
    "This document is a work order that `personaload report --handoff` generated from measured "
    "results. It is written so that a coding agent in another session can act on it alone.\n\n"
    "- personaload loads an OpenAI-compatible server with synthetic chat sessions (shared "
    "personas or instructions, growing history, optional retrieved documents) and reports the "
    "cost per 1M output tokens, per 1K turns and per session at the fastest load that still "
    "meets the SLO (TTFT and ITL p95, error rate).\n"
    "- personaload built the prompts, so the tasks below point at **server, engine and model "
    "settings**. The service's own prompt assembly was not measured.\n"
    "- SLO: upper limits on TTFT p95 (time to first token), ITL p95 (gap between streamed chunks) "
    "and the error rate. A stage that exceeds any of them is FAIL."
)


@dataclass(frozen=True)
class Rule:
    title: str
    causes: str
    todo: str
    check: str


RULES = {  # tasks are listed in this order
    "failed": Rule(
        "Stage failed",
        "The target server did not respond, every request of the stage failed, or AIPerf was "
        "misconfigured.",
        "Look for the cause at the end of `aiperf/logs/aiperf.log` and `aiperf.console.log` "
        "in that "
        "stage directory. First check that the server is up and answers `/v1/chat/completions`.",
        "Re-measure with the same scenario. Done when that stage ends with status ok.",
    ),
    "unjudged": Rule(
        "SLO cannot be judged (no request finished in the measured window, or the stage was cut)",
        "No request both started and finished inside the measured window (duration after the "
        "warmup). A single request took longer than the window (the server may be saturated, or "
        "hit target.timeout), or the stage was interrupted (partial). ITL is also N/A when every "
        "response arrived as a single chunk.",
        "Check `request_latency` and `error` in that stage's `aiperf/profile_export.jsonl`. If the "
        "server is saturated, this load is its limit: for a higher target load, scale the server, "
        "model or hardware; otherwise lengthen duration and re-measure. Re-run interrupted stages "
        "to the end.",
        "Re-measure. Done when the stage is judged PASS or FAIL.",
    ),
    "ttft_p95": Rule(
        "First token is slow (TTFT p95 over the limit)",
        "Prefill bottleneck: server prefix caching is off or misses, the server has too few "
        "concurrent slots, or the model is large for the hardware.",
        "Turn on server prefix caching (vLLM `--enable-prefix-caching`; on by default in "
        "llama.cpp) and check the cache hit rate; if it is N/A, first make the server report "
        "cached tokens (see that task). Raise the slot or batch limits, or use a smaller "
        "or quantized model, or faster hardware.",
        "Re-measure with the same scenario. Done when the stage in the evidence passes the whole "
        "SLO (TTFT, ITL and error rate) and the headline $/1M out is no worse than in this "
        "document. Improving one metric while another gets worse is not done.",
    ),
    "itl_p95": Rule(
        "Generation is slow (ITL p95 over the limit)",
        "Decode bottleneck: too many requests generate at once, prefill work interrupts decoding, "
        "or the hardware's memory bandwidth is short for the model.",
        "Lower the server's maximum concurrent batch, cut prefill work with prefix caching, or use "
        "a smaller or quantized model, or faster hardware.",
        "Re-measure with the same scenario. Done when the stage in the evidence passes the whole "
        "SLO (TTFT, ITL and error rate) and the headline $/1M out is no worse than in this "
        "document. Improving one metric while another gets worse is not done.",
    ),
    "error_rate": Rule(
        "Error rate over the limit",
        "The status codes tell the cause: 429 is a rate limit, 5xx is a server error or "
        "overload, null is a timeout or a dropped connection.",
        "Find the errors of the same time window in the server log. For limits or timeouts, fix "
        "the server or gateway settings; for overload, adjust the concurrency limit.",
        "Re-measure with the same scenario. Done when the stage in the evidence passes the whole "
        "SLO (TTFT, ITL and error rate) and the headline $/1M out is no worse than in this "
        "document. Improving one metric while another gets worse is not done.",
    ),
    "cache_unreported": Rule(
        "The server does not report cached tokens",
        "Responses have no `prompt_tokens_details.cached_tokens` in their usage, so the cache "
        "effect cannot be measured.",
        "Start vLLM with `--enable-prompt-tokens-details`. For other engines or APIs, check "
        "whether they support this field.",
        "Re-measure. Done when cache hit% is no longer N/A.",
    ),
    "cache_low": Rule(
        "The prefix cache rarely hits",
        "personaload workloads usually cache well (shared personas, growing history). "
        f"A cache hit rate below {LOW_CACHE_HIT:.0%} means server prefix caching is off, the cache "
        "is evicting, or the workload itself has little reuse (short session.turns and more "
        "personas than sessions, so most requests are first turns).",
        "First check session.turns and personas.count in scenario.resolved.yaml to rule out the "
        "workload. Otherwise check the server's prefix caching settings and give the KV cache more "
        "memory (vLLM `--gpu-memory-utilization`, llama.cpp `--ctx-size`).",
        f"Re-measure. Done when the cache hit rate is at least {LOW_CACHE_HIT:.0%}.",
    ),
    "usage": Rule(
        "The server does not return usage",
        "Streaming responses carry no usage, so output tokens were counted from chunks. "
        "Throughput and cost are inaccurate.",
        "Check whether the server supports `stream_options.include_usage`, and its version and "
        "settings.",
        "Re-measure. Done when the missing-usage warning is gone.",
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
        return [("failed", at + (first[0] if first else "unknown cause"))]
    found = []
    judged = s.get("slo") or {}
    verdict, violations = judged.get("verdict", NA), judged.get("violations") or []
    requests = s.get("requests") or {}
    if s.get("status") != "ok" or verdict not in (PASS, FAIL):
        n = requests.get("success", 0) + requests.get("error", 0)
        line = f"status {s.get('status', NA)}, SLO {verdict}, {n} requests in the window"
        found.append(("unjudged", at + line + f" ({s.get('records', NA)} records)"))
    ok = requests.get("success")
    sample = f", {ok} successful requests" if ok is not None else ""
    if "ttft_p95" in violations:
        ttft = _num((s.get("ttft_ms") or {}).get("p95"), ".0f")
        limit = f"limit {slo.get('ttft_p95', NA)}{sample}"
        found.append(("ttft_p95", at + f"TTFT p95 {ttft} ms ({limit})"))
    if "itl_p95" in violations:
        itl = _num((s.get("itl_ms") or {}).get("p95"), ".1f")
        limit = f"limit {slo.get('itl_p95', NA)}{sample}"
        found.append(("itl_p95", at + f"ITL p95 {itl} ms ({limit})"))
    if "error_rate" in violations:
        codes = ", ".join(f"{code}×{n}" for code, n in (s.get("error_codes") or {}).items())
        line = f"error rate {_pct(s.get('error_rate'))} (limit {_pct(slo.get('error_rate_max'))})"
        found.append(("error_rate", at + line + (f", codes {codes}" if codes else "")))
    cache = s.get("cache_hit") or {}
    if cache.get("eligible") and not cache.get("reported"):
        found.append(("cache_unreported", at + f"cached reported 0/{cache['eligible']}"))
    elif cache.get("rate") is not None and cache["rate"] < LOW_CACHE_HIT:
        counts = f"cached reported {cache['reported']}/{cache['eligible']}"
        found.append(("cache_low", at + f"cache hit {_pct(cache['rate'])} ({counts})"))
    if n := s.get("usage_missing"):
        found.append(("usage", at + f"{n} requests without usage"))
    return found


def _target_line(label: str, sc: dict) -> str:
    target, slo, cost = sc.get("target") or {}, sc.get("slo") or {}, sc.get("cost") or {}
    model = f"`{target['model']}`" if target.get("model") else NA
    url = f"`{target['base_url']}`" if target.get("base_url") else NA
    line = f"- {label}: model {model} @ {url}"
    if slo:
        line += (
            f", SLO TTFT p95 ≤ {slo.get('ttft_p95', NA)}, ITL p95 ≤ {slo.get('itl_p95', NA)}, "
            f"error rate ≤ {_pct(slo.get('error_rate_max'))}"
        )
    if cost:
        line += f", cost basis ${cost.get('gpu_hourly_usd', NA)}/h × {cost.get('gpu_count', 1)}"
        line += f" ({cost['label']})" if cost.get("label") else ""
    return line


def _tasks(evidence: dict[str, list[str]]) -> str:
    blocks = []
    for key, rule in RULES.items():
        if not evidence.get(key):
            continue
        lines = [f"### {len(blocks) + 1}. {rule.title}"]
        lines += [f"- Evidence: {e}" for e in evidence[key]]
        lines += [
            f"- Likely causes: {rule.causes}",
            f"- To do: {rule.todo}",
            f"- Done when: {rule.check}",
        ]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) or "None: every stage passed the SLO and nothing needs fixing."


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
                f"- {label}: headline out tok/s {tps:.1f}, {delta:+.0f}% vs {base_label} "
                "(compared on throughput because the cost is $0)"
            )
        elif base and usd is not None:
            delta = (usd / base - 1) * 100
            lines.append(f"- {label}: headline $/1M out ${usd:.3f}, {delta:+.0f}% vs {base_label}")
        else:
            lines.append(
                f"- {label}: cannot compare with {base_label} (no SLO-passing stage, or cost N/A)"
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
                at + f"only {n} successful requests in the window, "
                "so p95 is set by the top one or two. "
                "The verdict (PASS included) may be luck: lengthen duration and re-measure."
            )
    top = max(stages, key=lambda s: s["stage"], default=None)
    if top and top.get("status") == "ok" and (top.get("slo") or {}).get("verdict") == PASS:
        lines.append(
            f"- {label} stage {top['stage']:02d}: even the highest load passed, so saturation "
            "was not reached. The headline is capped by the sweep, the real $/1M out may be lower, "
            "and comparisons with it are less exact. Add higher load stages and re-measure."
        )
    return lines


def _rerun(current_dir: Path, has_scenario: bool) -> str:
    """Absolute, quoted commands that work from any directory and any shell PATH."""
    exe = f"{shlex.quote(sys.executable)} -m personaload"
    scenario = shlex.quote(str(current_dir / "scenario.resolved.yaml"))
    after = current_dir.parent / "after"  # next to the measured run
    out = shlex.quote(str(after))
    run = f"{exe} run {scenario} --out {out}"
    if not has_scenario:
        run = (
            "# no scenario.resolved.yaml: run the original scenario YAML\n"
            f"{exe} run <original-scenario.yaml> --out {out}"
        )
    new_run = shlex.quote(str(after)) + "/<new-results-dir>"
    handoff = shlex.quote(str(current_dir.parent / "handoff.md"))
    return f"{run}\n{exe} report {shlex.quote(str(current_dir))} {new_run} --handoff {handoff}"


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
        "\n\nThis is a sweep that raises the load stage by stage, so a FAIL above the headline "
        "stage may just be the server's saturation point. If the target concurrency (or arrival "
        "rate) is not known, ask the user first."
        if evidence and len(current_stages) > 1
        else ""
    )
    raw = [f"- {label}: `{path}`" for label, (path, _) in runs.items()]
    raw.append(
        "\nEach directory: `report.md` (stage table), `stage_NN/stage.json` (stage summary), "
        "`stage_NN/aiperf/profile_export.jsonl` (per-request records; absent with "
        "`record_requests: false`), `stage_NN/aiperf/logs/aiperf.log` (AIPerf log)."
    )
    parts = [
        f"# personaload handoff: {', '.join(labels)}",
        INTRO,
        "## Target",
        "\n".join(_target_line(label, scenarios[label]) for label in labels),
        "## Results",
        *[f"> Warning: {w}. This comparison is not fair." for w in mismatch],
        "\n".join(results),
        stage_table([(label if many else None, s) for label, (_, st) in runs.items() for s in st]),
        "## Tasks",
        f"Based on the last result ({current}).{sweep}",
        _tasks(evidence),
        "## Measurement quality warnings (not service-code fixes)",
        "\n".join(quality) or "none",
        "## How to re-measure",
        "After changing the settings, re-measure with the same scenario. The same scenario gives "
        "the same dataset (sha256), so the before/after comparison is fair.",
        f"```bash\n{rerun}\n```",
        "## Raw data",
        "\n".join(raw),
    ]
    return "\n\n".join(parts) + "\n"
