"""Measurement window, request metrics, active sessions, think lag and arrival diagnostics
(SPEC_B §9.2, §9.3, §9.6).

All times are ns on the host wall-clock axis AIPerf stamps records with; only differences
are used. Session plans come from the generator, so no dataset file is re-parsed.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from personaload.report.records import Record
from personaload.workload.sessions import SessionPlan

MS = 1_000_000
QUEUED_MS = 100.0
# segments shorter than this are session hand-offs, not load: a sub-ms gap, or a sub-ms overlap
# where the new credit_issued_ns precedes the old request_end_ns (different clocks, §9.1)
SHORT_GAP_NS = 10 * MS
PERCENTILES = (50, 90, 95, 99)
CLOCK_SKEW_MS = -5.0  # a lag below this means the host clock was stepped (§9.6)


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear interpolation between closest ranks (numpy 'linear'). None for no samples."""
    if not values:
        return None
    xs = sorted(values)
    pos = (len(xs) - 1) * q / 100
    lo = math.floor(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


@dataclass(frozen=True)
class Window:
    t0: int
    w: int  # warmup end
    e_eff: int  # min(stage end, t_stop)

    @property
    def empty(self) -> bool:
        return self.e_eff <= self.w


def _ns(ms: float) -> int:
    return round(ms * MS)


def _inside(ns: int | None, window: Window) -> bool:
    return ns is not None and window.w <= ns < window.e_eff


def _percentiles(values: Sequence[float], qs: Iterable[int] = PERCENTILES) -> dict:
    return {f"p{q}": percentile(values, q) for q in qs}


def output_tokens(r: Record) -> int:
    """usage.completion_tokens; without usage, data chunks (ITL gaps + 1), at least 1 (§9.3)."""
    if r.output_tokens is not None:
        return r.output_tokens
    return len(r.itl_ms) + 1


def request_metrics(records: Sequence[Record], window: Window, drain_cancelled: int | None) -> dict:
    """Latency, error rate and cache hit% over requests started in [W, E_eff); throughput over
    successful requests that ended there (§9.2, §9.3). Drain cancellations count as errors."""
    started = [r for r in records if _inside(r.request_start_ns, window)]
    ok = [r for r in started if not r.error]
    errors = [r for r in started if r.error]
    censored = drain_cancelled or 0
    attempts = len(started) + censored
    finished = [r for r in records if not r.error and _inside(r.request_end_ns, window)]
    seconds = (window.e_eff - window.w) / 1e9
    eligible = [r for r in ok if r.prompt_tokens is not None]
    reported = sum(r.cached_tokens is not None for r in eligible)
    prompt = sum(r.prompt_tokens for r in eligible)
    cached = sum(r.cached_tokens or 0 for r in eligible)
    codes = Counter("null" if r.status_code is None else str(r.status_code) for r in errors)
    return {
        "requests": {"success": len(ok), "error": len(errors)},
        "error_rate": (len(errors) + censored) / attempts if attempts else None,
        "error_codes": dict(sorted(codes.items())),
        "req_per_s": len(finished) / seconds,
        "out_tok_per_s": sum(output_tokens(r) for r in finished) / seconds,
        "usage_missing": sum(r.output_tokens is None for r in finished),
        "ttft_ms": _percentiles([r.ttft_ms for r in ok if r.ttft_ms is not None]),
        "itl_ms": _percentiles([gap for r in ok for gap in r.itl_ms]),  # pooled chunk gaps, D2
        "e2e_ms": _percentiles([r.e2e_ms for r in ok if r.e2e_ms is not None]),
        "aiperf_itl_ms": _percentiles(  # AIPerf per-request mean ITL: reference only
            [r.aiperf_itl_ms for r in ok if r.aiperf_itl_ms is not None], (50, 95)
        ),
        "cache_hit": {
            "rate": cached / prompt if reported and prompt else None,
            "reported": reported,
            "eligible": len(eligible),
        },
    }


def think_lag(records: Sequence[Record], plans: dict[str, SessionPlan], window: Window) -> dict:
    """start(t) - end(t-1) - delay_t for turn pairs of one session instance whose turn t starts in
    [W, E_eff). Pairs after an error turn are excluded: an HTTP error has end == start (§9.6)."""
    by_turn = {
        (r.session, r.turn_index): r
        for r in records
        if r.session is not None and r.turn_index is not None
    }
    lags, after_error = [], 0
    for (session, turn), r in by_turn.items():
        prev = by_turn.get((session, turn - 1))
        plan = plans.get(r.conversation_id)
        if turn == 0 or prev is None or not _inside(r.request_start_ns, window):
            continue
        if prev.error:
            after_error += 1
        elif prev.request_end_ns is not None and plan is not None and turn < len(plan.turns):
            gap_ms = (r.request_start_ns - prev.request_end_ns) / MS
            lags.append(gap_ms - (plan.turns[turn].delay_ms or 0.0))
    return _percentiles(lags, (50, 95, 99)) | {
        "pairs": len(lags),
        "excluded_after_error": after_error,
        "below_clock_floor": sum(lag < CLOCK_SKEW_MS for lag in lags),
    }


def reused_conversations(records: Iterable[Record]) -> int:
    """Conversations AIPerf replayed as 2+ session instances: the dataset ran out (§7.5)."""
    instances: dict[str, set[str]] = {}
    for r in records:
        if r.conversation_id is not None and r.session is not None:
            instances.setdefault(r.conversation_id, set()).add(r.session)
    return sum(len(sessions) > 1 for sessions in instances.values())


def measurement_window(
    records: Iterable[Record],
    plans: dict[str, SessionPlan],
    warmup_ms: float,
    duration_ms: float,
    t_stop_ns: int | None,
    arrival: bool,
) -> Window | None:
    """t0: earliest credit (concurrency) or earliest credit minus its schedule (arrival)."""
    candidates = []
    for r in records:
        if r.credit_issued_ns is None:
            continue
        if not arrival:
            candidates.append(r.credit_issued_ns)
        elif r.turn_index == 0 and r.conversation_id in plans:
            candidates.append(r.credit_issued_ns - _ns(plans[r.conversation_id].start_ms))
    if not candidates:
        return None
    t0 = min(candidates)
    w = t0 + _ns(warmup_ms)
    e = w + _ns(duration_ms)
    return Window(t0, w, e if t_stop_ns is None else min(e, t_stop_ns))


@dataclass(frozen=True)
class ActiveSessions:
    avg: float
    min: int
    max: int
    unobserved: int  # instances without a turn-0 record
    segments: tuple[tuple[int, int, int], ...]  # (start_ns, end_ns, count) over the window


def _spans(records: Iterable[Record], plans: dict[str, SessionPlan]) -> tuple[list, int]:
    by_session: dict[str, list[Record]] = {}
    for r in records:
        if r.session is not None and r.turn_index is not None:
            by_session.setdefault(r.session, []).append(r)
    spans, unobserved = [], 0
    for session_records in by_session.values():
        first = next((r for r in session_records if r.turn_index == 0), None)
        if first is None or first.credit_issued_ns is None:
            unobserved += 1
            continue
        plan = plans.get(first.conversation_id)
        last_turn = len(plan.turns) - 1 if plan else None
        last = next((r for r in session_records if r.turn_index == last_turn), None)
        # A session cut off by the stage end still holds its AIPerf slot until the phase ends.
        end = last.request_end_ns if last and last.request_end_ns is not None else math.inf
        spans.append((first.credit_issued_ns, end))
    return spans, unobserved


def active_sessions(
    records: Sequence[Record], plans: dict[str, SessionPlan], window: Window
) -> ActiveSessions:
    spans, unobserved = _spans(records, plans)
    events: dict[int, int] = {}
    for start, end in spans:
        lo, hi = max(start, window.w), min(end, window.e_eff)
        if lo < hi:
            events[lo] = events.get(lo, 0) + 1
            events[int(hi)] = events.get(int(hi), 0) - 1
    times = sorted({window.w, window.e_eff, *events})
    segments, count = [], 0
    for a, b in zip(times, times[1:], strict=False):  # every event lies inside [w, e_eff]
        count += events.get(a, 0)
        segments.append((a, b, count))
    total = window.e_eff - window.w
    avg = sum((b - a) * c for a, b, c in segments) / total
    steady = [c for a, b, c in segments if b - a >= SHORT_GAP_NS] or [c for _, _, c in segments]
    return ActiveSessions(avg, min(steady), max(steady), unobserved, tuple(segments))


def arrival_diagnostics(
    records: Sequence[Record],
    plans: dict[str, SessionPlan],
    window: Window,
    warmup_ms: float,
    duration_ms: float,
) -> dict:
    """Start lag of sessions scheduled inside the measured window (SPEC_B §9.6)."""
    targets = {cid for cid, p in plans.items() if warmup_ms <= p.start_ms < warmup_ms + duration_ms}
    seen = {r.conversation_id for r in records}
    lags = [
        (r.request_start_ns - window.t0 - _ns(plans[r.conversation_id].start_ms)) / MS
        for r in records
        if r.turn_index == 0 and r.conversation_id in targets and r.request_start_ns is not None
    ]
    return {
        "target_sessions": len(targets),
        "actual_rate": len(targets) / (duration_ms / 1000),
        "start_lag_ms": {f"p{q}": percentile(lags, q) for q in (50, 95, 99)},
        "queued": sum(lag > QUEUED_MS for lag in lags),
        "not_started": len(targets - seen),
    }
