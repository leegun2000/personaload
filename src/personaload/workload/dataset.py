"""AIPerf `multi_turn` JSONL serialization (SPEC_B §7.4).

Rules that AIPerf 0.12.0 enforces silently:
- the system turn carries only role+text, otherwise it is not hoisted into system_message;
- turn-level `max_tokens` is ignored, `output_length` is the per-turn output cap;
- fixed-schedule needs a top-level `timestamp` on the first record (we put it on every line).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator
from pathlib import Path

from personaload.workload.personas import Persona
from personaload.workload.sessions import SessionPlan, UserTurn


def _turn_record(turn: UserTurn) -> dict:
    record: dict = {"text": turn.text}
    if turn.timestamp_ms is not None:
        record["timestamp"] = turn.timestamp_ms
    if turn.delay_ms is not None:
        record["delay"] = turn.delay_ms
    record["output_length"] = turn.output_length
    return record


def session_record(plan: SessionPlan, persona: Persona) -> dict:
    record: dict = {"session_id": plan.session_id}
    if plan.start_ms is not None:
        record["timestamp"] = plan.start_ms
    record["turns"] = [
        {"role": "system", "text": persona.text},
        *(_turn_record(t) for t in plan.turns),
    ]
    return record


def dumps_line(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"))


def iter_lines(plans: Iterable[SessionPlan], personas: list[Persona]) -> Iterator[str]:
    for plan in plans:
        yield dumps_line(session_record(plan, personas[plan.persona_index]))


def write_dataset(path: Path, plans: Iterable[SessionPlan], personas: list[Persona]) -> str:
    """Write the JSONL file and return its sha256 hex digest."""
    digest = hashlib.sha256()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for line in iter_lines(plans, personas):
            data = line + "\n"
            fh.write(data)
            digest.update(data.encode("utf-8"))
    return digest.hexdigest()
