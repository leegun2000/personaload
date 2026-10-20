"""Reading AIPerf 0.12.0 artifacts: per-request records and log lines (SPEC_B §8.4, §9.1)."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

_PHASE_DONE = re.compile(
    r"Phase profiling \(profiling\) complete \| completed=([\d,]+), cancelled=([\d,]+), "
    r"errors=([\d,]+)(.*)"
)
_USER_CANCEL = re.compile(r"Sent cancellation requests for ([\d,]+) in-flight credits")
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Record:
    """One request. Any field may be None: AIPerf omits keys for synthesized error records."""

    conversation_id: str | None
    session: str | None  # x_correlation_id: one session *instance*
    turn_index: int | None
    credit_issued_ns: int | None
    request_start_ns: int | None
    request_end_ns: int | None
    error: bool
    status_code: int | None = None  # error.code; None for success or a code-less error
    ttft_ms: float | None = None
    e2e_ms: float | None = None
    itl_ms: tuple[float, ...] = ()  # gaps between data chunks; empty below 2 chunks
    prompt_tokens: int | None = None  # usage.prompt_tokens
    output_tokens: int | None = None  # usage.completion_tokens (reasoning included, D4)
    cached_tokens: int | None = None  # usage cached prompt tokens
    aiperf_itl_ms: float | None = None  # AIPerf per-request mean ITL, reference only (D2)


def _record(raw: dict) -> Record:
    meta = raw.get("metadata", {})
    metrics = raw.get("metrics", {})

    def value(name: str):
        return metrics.get(name, {}).get("value")

    error = raw.get("error")
    return Record(
        conversation_id=meta.get("conversation_id"),
        session=meta.get("x_correlation_id"),
        turn_index=meta.get("turn_index"),
        credit_issued_ns=meta.get("credit_issued_ns"),
        request_start_ns=meta.get("request_start_ns"),
        request_end_ns=meta.get("request_end_ns"),
        error=error is not None,
        status_code=error.get("code") if error else None,
        ttft_ms=value("time_to_first_token"),
        e2e_ms=value("request_latency"),
        itl_ms=tuple(value("inter_chunk_latency") or ()),
        prompt_tokens=value("usage_prompt_tokens"),
        output_tokens=value("usage_completion_tokens"),
        cached_tokens=value("usage_prompt_cache_read_tokens"),
        aiperf_itl_ms=value("inter_token_latency"),
    )


def load_records(path: Path) -> list[Record]:
    """Profiling-phase records of profile_export.jsonl (personaload runs no AIPerf warmup)."""
    records, bad = [], 0
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:  # a line cut short by SIGKILL
                bad += 1
                continue
            if raw.get("metadata", {}).get("benchmark_phase", "profiling") == "profiling":
                records.append(_record(raw))
    if bad:
        log.warning("%s: skipped %d unreadable lines", path, bad)
    return records


@dataclass(frozen=True)
class Cancellations:
    drain: int | None  # cancelled after drain_timeout; None = line not found (unknown)
    user: int | None  # cancelled by Ctrl+C (partial stage)


def parse_cancellations(log_text: str) -> Cancellations:
    """From the phase-complete NOTICE. The 'timed out' WARNING's cancelled= is a pre-cancel
    snapshot, and the 'sessions: ... cancelled=' value counts sessions, so neither is used.
    After Ctrl+C AIPerf 0.12.0 logs no phase-complete line, only the router's cancel count
    (at INFO, and only when something was in flight)."""
    match = _PHASE_DONE.search(log_text)
    if not match:
        sent = _USER_CANCEL.search(log_text)
        return Cancellations(drain=None, user=int(sent.group(1).replace(",", "")) if sent else None)
    cancelled = int(match.group(2).replace(",", ""))
    if "was_cancelled=True" in match.group(4):
        return Cancellations(drain=None, user=cancelled)
    return Cancellations(drain=cancelled, user=None)
