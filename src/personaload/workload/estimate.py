"""Scenario checks that need the workload model (SPEC_B §6.3).

Errors, warnings and per-stage size estimates.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from personaload.config import ArrivalStage, Dist, Scenario
from personaload.workload.dists import PersonaPicker, clamped_mean, configured_mean
from personaload.workload.personas import Persona
from personaload.workload.sessions import WorkloadError, stage_session_count
from personaload.workload.text import CHARS_PER_TOKEN, make_text

MAX_DATASET_BYTES = 1 << 30
THINK_WARN_MS = 60_000.0
MEAN_DEVIATION_WARN = 0.05
# JSON scaffolding per record, measured from generated lines / AIPerf inputs.json payloads.
_TURN_OVERHEAD = 60
_LINE_OVERHEAD = 70
_PAYLOAD_OVERHEAD = 260


@dataclass(frozen=True)
class DistMean:
    name: str
    configured: float | None
    clamped: float
    is_duration: bool


@dataclass(frozen=True)
class StageEstimate:
    stage_no: int
    sessions: int
    dataset_bytes: int
    inputs_json_bytes: int


@dataclass(frozen=True)
class Checks:
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    means: tuple[DistMean, ...]
    stages: tuple[StageEstimate, ...]


def _dists(scenario: Scenario) -> list[tuple[str, Dist, bool]]:
    session = scenario.session
    dists = [
        ("session.turns", session.turns, False),
        ("session.user_input_tokens", session.user_input_tokens, False),
        ("session.max_output_tokens", session.max_output_tokens, False),
        ("session.think_time", session.think_time, True),
    ]
    if session.retrieved_tokens is not None:
        dists.append(("session.retrieved_tokens", session.retrieved_tokens, False))
    if scenario.personas.system_prompt_tokens is not None:
        dists.insert(
            0, ("personas.system_prompt_tokens", scenario.personas.system_prompt_tokens, False)
        )
    return dists


def dist_means(scenario: Scenario) -> list[DistMean]:
    return [DistMean(n, configured_mean(d), clamped_mean(d), dur) for n, d, dur in _dists(scenario)]


def _bytes_per_char(language: str) -> float:
    sample = make_text(random.Random(0), 2000, language)
    return len(sample.encode("utf-8")) / len(sample)


def _mean_persona_bytes(scenario: Scenario, personas: list[Persona]) -> float:
    session = scenario.session
    probs = PersonaPicker(len(personas), session.persona_selection, session.zipf_s).probabilities()
    return sum(
        p * len(persona.text.encode("utf-8")) for p, persona in zip(probs, personas, strict=True)
    )


def _session_count(scenario: Scenario, stage_no: int, per_session_bytes: float) -> int:
    stage = scenario.load.stages[stage_no - 1]
    if isinstance(stage, ArrivalStage):
        # A typo in sessions_per_sec/duration must hit the size error, not materialize
        # millions of offsets first. Far above the limit, the Poisson mean is precise enough.
        expected = 1 + stage.sessions_per_sec * (stage.warmup + stage.duration) / 1000
        if expected * per_session_bytes > 2 * MAX_DATASET_BYTES:
            return round(expected)
    return stage_session_count(scenario, stage_no)


def estimate_stage(scenario: Scenario, personas: list[Persona], stage_no: int) -> StageEstimate:
    session = scenario.session
    turns = clamped_mean(session.turns)
    retrieved = clamped_mean(session.retrieved_tokens) if session.retrieved_tokens else 0
    user_bytes = (
        (clamped_mean(session.user_input_tokens) + retrieved)
        * CHARS_PER_TOKEN[session.language]
        * _bytes_per_char(session.language)
    )
    persona_bytes = _mean_persona_bytes(scenario, personas)
    per_session = persona_bytes + turns * (user_bytes + _TURN_OVERHEAD) + _LINE_OVERHEAD
    per_payload = persona_bytes + user_bytes + _PAYLOAD_OVERHEAD
    sessions = _session_count(scenario, stage_no, per_session)
    return StageEstimate(
        stage_no=stage_no,
        sessions=sessions,
        dataset_bytes=round(sessions * per_session),
        inputs_json_bytes=round(sessions * turns * per_payload),
    )


def _warnings(scenario: Scenario, means: list[DistMean]) -> list[str]:
    warnings = []
    think = scenario.session.think_time
    think_max = think.value if think.dist == "constant" else think.max
    if think_max > THINK_WARN_MS:
        warnings.append("session.think_time 최대가 60s를 넘는다. AIPerf도 로드 시 한 번 경고한다")
    if scenario.load.drain_timeout < scenario.target.timeout:
        warnings.append(
            "load.drain_timeout < target.timeout: drain 뒤 취소되는 요청은 레코드 없이 검열된다"
        )
    for m in means:
        if m.configured and abs(m.clamped - m.configured) / m.configured > MEAN_DEVIATION_WARN:
            warnings.append(
                f"{m.name}: 클램프 후 평균({m.clamped:.4g})이 설정 평균({m.configured:.4g})과 "
                f"{MEAN_DEVIATION_WARN:.0%} 넘게 다르다"
            )
    return warnings


def check_scenario(scenario: Scenario, personas: list[Persona]) -> Checks:
    means = dist_means(scenario)
    errors: list[str] = []
    stages: list[StageEstimate] = []
    for stage_no in range(1, len(scenario.load.stages) + 1):
        try:
            estimate = estimate_stage(scenario, personas, stage_no)
        except WorkloadError as exc:
            errors.append(f"load.stages.{stage_no - 1}: {exc}")
            continue
        if estimate.dataset_bytes > MAX_DATASET_BYTES:
            gib = estimate.dataset_bytes / 2**30
            errors.append(
                f"load.stages.{stage_no - 1}: 예상 dataset.jsonl {gib:.2f} GiB > 1 GiB. "
                "세션 수(session_pool_factor, sessions)나 페르소나 길이를 줄인다"
            )
        stages.append(estimate)
    return Checks(tuple(errors), tuple(_warnings(scenario, means)), tuple(means), tuple(stages))
