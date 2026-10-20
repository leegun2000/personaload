"""Persona pool: seeded synthetic personas or a JSONL file (SPEC_B §7.2)."""

from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import dataclass
from pathlib import Path

from personaload.config import PersonasConfig
from personaload.workload.dists import draw_int
from personaload.workload.rng import child_rng
from personaload.workload.text import CHARS_PER_TOKEN, make_text, target_chars


class PersonaError(Exception):
    pass


@dataclass(frozen=True)
class Persona:
    index: int
    name: str
    text: str


_NAMES = {
    "en": (
        ("Mira", "Theo", "Ari", "June", "Kai", "Nell", "Rowan", "Isla", "Felix", "Wren"),
        ("Voss", "Hale", "Quill", "Ashby", "Marlow", "Reyes", "Thorne", "Lark", "Sato", "Vale"),
    ),
    "ko": (
        ("서윤", "도현", "하린", "지호", "유나", "시우", "다온", "은재", "태오", "소이"),
        ("한", "강", "윤", "서", "문", "백", "류", "진", "송", "하"),
    ),
}
_TRAITS = {
    "en": (
        "warm",
        "sarcastic",
        "curious",
        "stubborn",
        "gentle",
        "reckless",
        "shy",
        "proud",
        "playful",
        "melancholic",
        "loyal",
        "mischievous",
    ),
    "ko": (
        "다정한",
        "냉소적인",
        "호기심 많은",
        "고집 센",
        "온화한",
        "무모한",
        "수줍은",
        "자존심 강한",
        "장난스러운",
        "우울한",
        "의리 있는",
        "짓궂은",
    ),
}
_STYLES = {
    "en": (
        "speaks in short nautical metaphors",
        "answers with a question first",
        "uses old-fashioned formal words",
        "teases the user gently",
        "whispers secrets dramatically",
        "never uses contractions",
    ),
    "ko": (
        "짧은 바다 비유로 말한다",
        "질문으로 먼저 되묻는다",
        "예스러운 존댓말을 쓴다",
        "사용자를 살짝 놀린다",
        "비밀을 극적으로 속삭인다",
        "반말과 존댓말을 섞는다",
    ),
}
_LABELS = {
    "en": ("Name", "Personality", "Speech style", "World"),
    "ko": ("이름", "성격", "말투", "세계관"),
}


def _pick(rng: random.Random, seq: tuple[str, ...]) -> str:
    return seq[math.floor(rng.random() * len(seq))]


def _synthetic(config: PersonasConfig, seed: int, index: int, language: str) -> Persona:
    rng = child_rng(seed, "persona", index)
    first, last = _NAMES[language]
    given, family = _pick(rng, first), _pick(rng, last)
    name = f"{family}{given}" if language == "ko" else f"{given} {family}"
    # dict.fromkeys dedups in draw order; sets are banned in workload code (SPEC_B §7.1)
    traits = ", ".join(dict.fromkeys(_pick(rng, _TRAITS[language]) for _ in range(3)))
    label_name, label_traits, label_style, label_world = _LABELS[language]
    body = (
        f"[Persona P{index + 1:04d} · {name}]\n"
        f"{label_name}: {name}\n"
        f"{label_traits}: {traits}\n"
        f"{label_style}: {_pick(rng, _STYLES[language])}\n"
        f"{label_world}: "
    )
    goal = target_chars(draw_int(config.system_prompt_tokens, rng), language)
    missing = goal - len(body)
    if missing > 0:
        body += make_text(rng, max(1, math.ceil(missing / CHARS_PER_TOKEN[language])), language)
    return Persona(index=index, name=name, text=body)


def synthetic_personas(config: PersonasConfig, seed: int, language: str) -> list[Persona]:
    return [_synthetic(config, seed, i, language) for i in range(config.count)]


def file_personas(path: Path, count: int | None) -> list[Persona]:
    try:
        # split on "\n" only: str.splitlines() also breaks on U+2028/U+0085, which JSON
        # allows raw inside strings (AIPerf reads JSONL the same way)
        lines = path.read_text(encoding="utf-8").split("\n")
    except (OSError, UnicodeDecodeError) as exc:
        raise PersonaError(f"cannot read the persona file: {exc}") from exc
    personas: list[Persona] = []
    for lineno, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PersonaError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
        if not isinstance(row, dict) or not isinstance(row.get("system_prompt"), str):
            raise PersonaError(f'{path}:{lineno}: not a {{"name", "system_prompt"}} object')
        if not row["system_prompt"].strip():
            continue
        try:
            row["system_prompt"].encode("utf-8")
        except UnicodeEncodeError as exc:
            raise PersonaError(
                f"{path}:{lineno}: characters that cannot be encoded as UTF-8 (lone surrogates)"
            ) from exc
        personas.append(Persona(len(personas), str(row.get("name", "")), row["system_prompt"]))
        if count is not None and len(personas) == count:
            break
    if not personas:
        raise PersonaError(f"{path}: no line with a non-empty system_prompt")
    return personas


def load_personas(config: PersonasConfig, seed: int, language: str) -> list[Persona]:
    if config.source == "file":
        return file_personas(Path(config.file), config.count)
    return synthetic_personas(config, seed, language)


def file_sha256(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
