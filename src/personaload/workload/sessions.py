"""Per-stage session plans: counts, arrival offsets and turn contents (SPEC_B §7.3, §7.5)."""

from __future__ import annotations

import math
from dataclasses import dataclass

from personaload.config import ArrivalStage, ConcurrencyStage, Scenario
from personaload.workload.dists import PersonaPicker, clamped_mean, draw, draw_int
from personaload.workload.rng import child_rng
from personaload.workload.text import make_text

MIN_SESSION_THINK_MS = 1000.0
RAG_FORMAT = {
    "ko": "[참고 문서]\n{doc}\n\n[질문]\n{question}",
    "en": "[Retrieved documents]\n{doc}\n\n[Question]\n{question}",
}


class WorkloadError(Exception):
    pass


@dataclass(frozen=True)
class UserTurn:
    text: str
    output_length: int
    delay_ms: float | None = None
    timestamp_ms: float | None = None


@dataclass(frozen=True)
class SessionPlan:
    session_id: str
    persona_index: int
    turns: tuple[UserTurn, ...]

    @property
    def start_ms(self) -> float | None:
        return self.turns[0].timestamp_ms


def session_think_ms(scenario: Scenario) -> float:
    """L = (E[T]-1) * E[think]: expected think time a session spends, in ms (SPEC_B §7.5)."""
    session = scenario.session
    return (clamped_mean(session.turns) - 1) * clamped_mean(session.think_time)


def concurrency_session_count(scenario: Scenario, stage: ConcurrencyStage) -> int:
    if stage.sessions is not None:
        return stage.sessions
    think_ms = session_think_ms(scenario)
    if think_ms < MIN_SESSION_THINK_MS:
        raise WorkloadError(
            f"세션당 기대 think time L={think_ms / 1000:.3f}s < 1s: 세션 수를 산정할 수 없다. "
            "turns·think_time을 늘리거나 단계에 sessions를 지정한다"
        )
    horizon_ms = stage.warmup + stage.duration
    factor = scenario.load.session_pool_factor
    return math.ceil(factor * stage.concurrency * horizon_ms / think_ms) + stage.concurrency


def arrival_offsets_ms(seed: int, stage_no: int, stage: ArrivalStage) -> list[float]:
    """Poisson session starts on [0, warmup+duration); the first is pinned to 0."""
    rng = child_rng(seed, "arrival", stage_no)
    horizon_s = (stage.warmup + stage.duration) / 1000
    offsets, t = [], 0.0
    while t < horizon_s:
        offsets.append(round(t * 1000, 1))
        t += -math.log(1 - rng.random()) / stage.sessions_per_sec
    return offsets


def stage_session_count(scenario: Scenario, stage_no: int) -> int:
    stage = scenario.load.stages[stage_no - 1]
    if isinstance(stage, ConcurrencyStage):
        return concurrency_session_count(scenario, stage)
    return len(arrival_offsets_ms(scenario.seed, stage_no, stage))


def _session_id(stage_no: int, index: int, width: int) -> str:
    return f"s{stage_no:02d}-{index:0{width}d}"


def _build_session(
    scenario: Scenario,
    picker: PersonaPicker,
    stage_no: int,
    index: int,
    width: int,
    start_ms: float | None,
) -> SessionPlan:
    session = scenario.session
    rng = child_rng(scenario.seed, "session", stage_no, index)
    persona_index = picker.pick(rng)
    # own stream: RAG on/off runs share personas, turns, inputs, lengths and think times
    docs = (
        child_rng(scenario.seed, "retrieval", stage_no, index) if session.retrieved_tokens else None
    )
    turns = []
    for t in range(draw_int(session.turns, rng)):
        text = make_text(rng, draw_int(session.user_input_tokens, rng), session.language)
        output_length = draw_int(session.max_output_tokens, rng)
        delay = round(draw(session.think_time, rng), 1) if t > 0 else None
        if docs is not None:
            doc = make_text(docs, draw_int(session.retrieved_tokens, docs), session.language)
            text = RAG_FORMAT[session.language].format(doc=doc, question=text)
        timestamp = start_ms if t == 0 else None
        turns.append(UserTurn(text, output_length, delay, timestamp))
    return SessionPlan(_session_id(stage_no, index, width), persona_index, tuple(turns))


def build_stage_sessions(
    scenario: Scenario, persona_count: int, stage_no: int
) -> list[SessionPlan]:
    """Sessions for 1-based stage `stage_no`. Session j depends only on (seed, stage, j)."""
    stage = scenario.load.stages[stage_no - 1]
    session = scenario.session
    picker = PersonaPicker(persona_count, session.persona_selection, session.zipf_s)
    if isinstance(stage, ArrivalStage):
        starts: list[float | None] = list(arrival_offsets_ms(scenario.seed, stage_no, stage))
    else:
        starts = [None] * concurrency_session_count(scenario, stage)
    width = max(6, len(str(len(starts))))
    return [
        _build_session(scenario, picker, stage_no, j, width, start)
        for j, start in enumerate(starts, 1)
    ]
