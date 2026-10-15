"""Scenario schema, defaults and validation (SPEC_B §6).

Durations are held as float milliseconds and serialized back to the shortest exact
duration string, so a dumped scenario re-validates to the same values.
"""

from __future__ import annotations

import io
import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    PlainSerializer,
    StrictInt,
    ValidationError,
    model_validator,
)
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)(ms|s|m|h)$")
_UNIT_MS = {"h": 3_600_000, "m": 60_000, "s": 1000, "ms": 1}
_NAME_RE = r"^[A-Za-z0-9._-]+$"


class ScenarioError(Exception):
    """Invalid scenario file. `errors` holds one human-readable line per problem."""

    def __init__(self, errors: list[str]):
        super().__init__("\n".join(errors))
        self.errors = errors


def parse_duration_ms(value: Any) -> float:
    if not isinstance(value, str):
        raise ValueError("기간은 '100ms', '30s', '3m', '1h' 형식의 문자열이어야 한다")
    match = _DURATION_RE.match(value)
    if not match:
        raise ValueError(f"기간 형식이 아니다: {value!r} (예: '100ms', '30s', '3m', '1h')")
    # Decimal keeps '2.01s' at exactly 2010.0; float('2.01') * 1000 would be 2009.999...
    return float(Decimal(match.group(1)) * _UNIT_MS[match.group(2)])


def format_duration_ms(ms: float) -> str:
    """Shortest duration string that parses back to exactly `ms`."""
    for unit in ("h", "m", "s"):
        quotient = ms / _UNIT_MS[unit]
        if quotient >= 1 and quotient.is_integer():
            return f"{int(quotient)}{unit}"
    if float(ms).is_integer():
        return f"{int(ms)}ms"
    return format(Decimal(repr(ms)), "f") + "ms"


Ms = Annotated[
    float,
    BeforeValidator(parse_duration_ms),
    PlainSerializer(format_duration_ms, return_type=str),
]

# NaN/Inf slip past hand-written comparisons (every `x < 0` is False for NaN), so reject them
# at parse time. Integer fields use StrictInt: YAML `true` must not become concurrency 1.
_STRICT = ConfigDict(extra="forbid", allow_inf_nan=False)


# --- distributions (SPEC_B §6.2) -------------------------------------------------------

_REQUIRED = {
    "uniform": ("min", "max"),
    "lognormal": ("mean", "stddev"),
    "exponential": ("mean",),
    "constant": ("value",),
}
_ALLOWED = {
    "uniform": {"min", "max"},
    "lognormal": {"min", "max", "mean", "stddev"},
    "exponential": {"min", "max", "mean"},
    "constant": {"min", "max", "value"},
}
_PARAMS = ("min", "max", "mean", "stddev", "value")


class Dist(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    dist: Literal["uniform", "lognormal", "exponential", "constant"] = "uniform"

    @model_validator(mode="after")
    def _check(self) -> Dist:
        missing = [f for f in _REQUIRED[self.dist] if getattr(self, f) is None]
        if missing:
            raise ValueError(f"dist={self.dist}에는 {', '.join(missing)}가 필요하다")
        unused = [
            f for f in _PARAMS if getattr(self, f) is not None and f not in _ALLOWED[self.dist]
        ]
        if unused:
            raise ValueError(f"dist={self.dist}에서는 {', '.join(unused)}를 쓰지 않는다")
        if any(getattr(self, f) is not None and getattr(self, f) < 0 for f in _PARAMS):
            raise ValueError("분포 값은 0 이상이어야 한다")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("min ≤ max여야 한다")
        if self.dist in ("lognormal", "exponential") and self.mean <= 0:
            raise ValueError("mean > 0이어야 한다")
        if self.dist == "constant" and not self._within(self.value):
            raise ValueError("constant의 value는 [min, max] 안이어야 한다")
        return self

    def _within(self, x: float) -> bool:
        return (self.min is None or x >= self.min) and (self.max is None or x <= self.max)

    def lower_bound(self) -> float:
        """Smallest value the distribution can produce after clamping."""
        if self.dist == "constant":
            return self.value
        return self.min if self.min is not None else 0.0


class CountDist(Dist):
    """Integer quantity (turns, tokens). uniform draws integers in [min, max]."""

    min: float | None = None
    max: float | None = None
    mean: float | None = None
    stddev: float | None = None
    value: float | None = None

    @model_validator(mode="after")
    def _integer_bounds(self) -> CountDist:
        # Deterministic values must be integers too: a constant 1.4 would be reported (and
        # sized) as 1.4 while every draw rounds to 1.
        fixed = ["min", "max", "value"]
        if self.dist == "lognormal" and self.stddev == 0:
            fixed.append("mean")
        for name in fixed:
            v = getattr(self, name)
            if v is not None and not float(v).is_integer():
                raise ValueError(f"{name}는 정수여야 한다")
        return self


class DurationDist(Dist):
    min: Ms | None = None
    max: Ms | None = None
    mean: Ms | None = None
    stddev: Ms | None = None
    value: Ms | None = None


def _require_min_one(dist: CountDist, field: str) -> None:
    if dist.lower_bound() < 1:
        raise ValueError(f"{field}의 최솟값은 1 이상이어야 한다 (min을 1 이상으로 지정)")


# --- sections -----------------------------------------------------------------------------


class TargetConfig(BaseModel):
    model_config = _STRICT

    base_url: str = Field(min_length=1)
    model: str = Field(min_length=1)
    api_key_env: str | None = None
    timeout: Ms = 120_000.0
    tokenizer: str = "builtin"
    max_tokens_field: Literal["max_completion_tokens", "max_tokens"] = "max_completion_tokens"
    extra_body: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> TargetConfig:
        if self.timeout <= 0:
            raise ValueError("timeout > 0이어야 한다")
        try:
            json.dumps(self.extra_body, allow_nan=False)
        except ValueError as exc:
            msg = "extra_body는 JSON으로 직렬화 가능해야 한다 (NaN/Infinity 불가)"
            raise ValueError(msg) from exc
        return self


class PersonasConfig(BaseModel):
    model_config = _STRICT

    count: StrictInt | None = Field(default=None, ge=1)
    system_prompt_tokens: CountDist | None = None
    source: Literal["synthetic", "file"] = "synthetic"
    file: str | None = None

    @model_validator(mode="after")
    def _check(self) -> PersonasConfig:
        if self.source == "synthetic":
            if self.count is None or self.system_prompt_tokens is None:
                raise ValueError("source=synthetic에는 count와 system_prompt_tokens가 필요하다")
            _require_min_one(self.system_prompt_tokens, "system_prompt_tokens")
        elif self.file is None:
            raise ValueError("source=file에는 file이 필요하다")
        return self


class SessionConfig(BaseModel):
    model_config = _STRICT

    language: Literal["en", "ko"] = "en"
    turns: CountDist
    user_input_tokens: CountDist
    max_output_tokens: CountDist
    think_time: DurationDist
    retrieved_tokens: CountDist | None = None  # RAG: a new document in front of every user turn
    persona_selection: Literal["uniform", "zipf"] = "uniform"
    zipf_s: float = 1.0

    @model_validator(mode="after")
    def _check(self) -> SessionConfig:
        for name in ("turns", "user_input_tokens", "max_output_tokens"):
            _require_min_one(getattr(self, name), name)
        if self.retrieved_tokens is not None:
            _require_min_one(self.retrieved_tokens, "retrieved_tokens")
        if self.think_time.max is None and self.think_time.dist != "constant":
            raise ValueError("think_time에는 max가 필요하다")
        if self.persona_selection == "zipf" and self.zipf_s <= 0:
            raise ValueError("persona_selection=zipf이면 zipf_s > 0이어야 한다")
        return self


class _StageBase(BaseModel):
    model_config = _STRICT

    duration: Ms
    warmup: Ms = 0.0

    @model_validator(mode="after")
    def _check_duration(self) -> _StageBase:
        if self.duration <= 0:
            raise ValueError("duration > 0이어야 한다")
        return self


class ConcurrencyStage(_StageBase):
    kind: Literal["concurrency"] = Field(default="concurrency", exclude=True)
    concurrency: StrictInt = Field(ge=1)
    sessions: StrictInt | None = Field(default=None, ge=1)


class ArrivalStage(_StageBase):
    kind: Literal["arrival_rate"] = Field(default="arrival_rate", exclude=True)
    sessions_per_sec: float = Field(gt=0)
    max_concurrency: StrictInt = Field(ge=1)


Stage = Annotated[ConcurrencyStage | ArrivalStage, Field(discriminator="kind")]


class LoadConfig(BaseModel):
    model_config = _STRICT

    mode: Literal["concurrency", "arrival_rate"]
    drain_timeout: Ms
    session_pool_factor: float = Field(default=3.0, ge=1)
    stages: list[Stage] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _tag_stages(cls, data: Any) -> Any:
        # The mode decides the stage schema; tagging each stage turns a wrong-mode field
        # into a precise "extra field not permitted" error instead of a union mismatch.
        if not isinstance(data, dict) or not isinstance(data.get("stages"), list):
            return data
        mode = data.get("mode")
        tagged = [{**s, "kind": mode} if isinstance(s, dict) else s for s in data["stages"]]
        return {**data, "stages": tagged}


class SloConfig(BaseModel):
    model_config = _STRICT

    ttft_p95: Ms
    itl_p95: Ms
    error_rate_max: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _check(self) -> SloConfig:
        if self.ttft_p95 <= 0 or self.itl_p95 <= 0:
            raise ValueError("ttft_p95와 itl_p95는 0보다 커야 한다")
        return self


class CostConfig(BaseModel):
    model_config = _STRICT

    gpu_hourly_usd: float = Field(ge=0)
    gpu_count: StrictInt = Field(default=1, ge=1)
    label: str = ""


class OutputConfig(BaseModel):
    model_config = _STRICT

    dir: str = "results/"
    prometheus_port: StrictInt | None = Field(default=None, ge=1, le=65535)
    prometheus_host: str = "127.0.0.1"
    record_requests: bool = True


class Scenario(BaseModel):
    model_config = _STRICT

    name: str = Field(pattern=_NAME_RE)
    seed: StrictInt = 0
    target: TargetConfig
    personas: PersonasConfig
    session: SessionConfig
    load: LoadConfig
    slo: SloConfig
    cost: CostConfig
    output: OutputConfig = Field(default_factory=OutputConfig)

    @model_validator(mode="before")
    @classmethod
    def _default_drain(cls, data: Any) -> Any:
        # D6: drain_timeout defaults to the request timeout, so requests started before the
        # stage end finish (or time out) with a record instead of being silently cancelled.
        if not isinstance(data, dict) or not isinstance(data.get("load"), dict):
            return data
        if "drain_timeout" in data["load"]:
            return data
        target = data.get("target")
        timeout = target.get("timeout", "120s") if isinstance(target, dict) else "120s"
        return {**data, "load": {**data["load"], "drain_timeout": timeout}}


# --- loading / dumping -------------------------------------------------------------------


def _format_loc(loc: tuple[Any, ...]) -> str:
    # Drop the discriminator tag pydantic inserts after a stage index: stages.0.<tag>.field
    parts = [
        str(p)
        for i, p in enumerate(loc)
        if not (i > 0 and isinstance(loc[i - 1], int) and p in ("concurrency", "arrival_rate"))
    ]
    return ".".join(parts) or "(root)"


def _format_errors(exc: ValidationError) -> list[str]:
    return [
        f"{_format_loc(err['loc'])}: {err['msg'].removeprefix('Value error, ')}"
        for err in exc.errors()
    ]


def scenario_from_dict(data: Any) -> Scenario:
    try:
        return Scenario.model_validate(data)
    except ValidationError as exc:
        raise ScenarioError(_format_errors(exc)) from exc


def load_scenario(path: str | Path) -> Scenario:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ScenarioError([f"시나리오 파일을 읽을 수 없다: {exc}"]) from exc
    try:
        data = YAML(typ="safe").load(text)
    except (YAMLError, ValueError) as exc:  # ruamel raises ValueError for huge ints
        raise ScenarioError([f"YAML 파싱 실패: {exc}"]) from exc
    scenario = scenario_from_dict(data)
    if scenario.personas.file is None:
        return scenario
    # scenario.resolved.yaml copies this path, and it must re-run from any directory
    absolute = str(Path(scenario.personas.file).resolve())
    return scenario.model_copy(
        update={"personas": scenario.personas.model_copy(update={"file": absolute})}
    )


def dump_scenario(scenario: Scenario, header: str = "") -> str:
    yaml = YAML()
    yaml.default_flow_style = False
    buf = io.StringIO()
    if header:
        buf.write(header)
    yaml.dump(scenario.model_dump(mode="json", exclude_none=True), buf)
    return buf.getvalue()
