"""aiohttp OpenAI-compatible `/v1/chat/completions` mock (SPEC_B §11).

Deterministic per request: output length and error injection are derived from sha256 of the
seed and the request messages, so the same request behaves the same after a restart.
Latency jitter uses one seeded stream per process and is not meant to be reproducible.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import logging
import math
import random
import time
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web

from personaload.mockserver.cache import Message, PrefixCache, message_tokens, prefix_digests

log = logging.getLogger("personaload.mockserver")

MODEL_ID = "mock-model"
DEFAULT_MAX_TOKENS = 256
MAX_BODY_BYTES = 1 << 30  # aiohttp's 1 MiB default is below a 30-turn Korean long-context body
SHUTDOWN_TIMEOUT_S = 1.0
# Each output token is one 4-char piece, so the reply re-counts to the same token total when
# it comes back as assistant history (ceil(len/4)).
PIECES = ("the ", "sea ", "and ", "old ", "map ", "tea ", "sky ", "was ", "red ", "fox ")


@dataclass(frozen=True)
class MockConfig:
    host: str = "127.0.0.1"
    port: int = 8000
    ttft_ms: float = 200.0
    tps: float = 40.0
    jitter: float = 0.0
    error_rate: float = 0.0
    cache_ttft_factor: float = 1.0
    seed: int = 0
    usage: bool = True

    def __post_init__(self) -> None:
        errors = []
        if not 0 <= self.jitter < 1:
            errors.append("jitter는 0 이상 1 미만이어야 한다")
        if not self.tps > 0:
            errors.append("tps는 0보다 커야 한다")
        if not self.ttft_ms >= 0:
            errors.append("ttft는 0 이상이어야 한다")
        if not self.cache_ttft_factor >= 0:
            errors.append("cache-ttft-factor는 0 이상이어야 한다")
        if self.error_rate > 1:
            errors.append("error-rate는 비율(0~1)이다. AIPerf mock의 백분율과 다르다")
        elif not self.error_rate >= 0:
            errors.append("error-rate는 0 이상이어야 한다")
        if not 1 <= self.port <= 65535:
            errors.append("port는 1~65535여야 한다")
        if errors:
            raise ValueError("\n".join(errors))


@dataclass
class _State:
    config: MockConfig
    cache: PrefixCache = field(default_factory=PrefixCache)
    ids: itertools.count = field(default_factory=itertools.count)
    jitter_rng: random.Random = field(init=False)

    def __post_init__(self) -> None:
        self.jitter_rng = random.Random(self.config.seed)

    def jittered(self, value: float) -> float:
        j = self.config.jitter
        return value * (1 + j * (2 * self.jitter_rng.random() - 1)) if j else value


STATE = web.AppKey("state", _State)


class BadRequest(ValueError):
    pass


def request_hash(messages: list[Message]) -> str:
    canonical = json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _request_rng(seed: int, req_hash: str, purpose: str) -> random.Random:
    digest = hashlib.sha256(f"{seed}:{req_hash}:{purpose}".encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def output_budget(body: dict[str, Any], seed: int, req_hash: str) -> tuple[int, int]:
    """(cap, tokens to generate).

    Exactly cap with ignore_eos, else uniform on [ceil(cap/2), cap].
    """
    cap = body.get("max_completion_tokens")
    if cap is None:
        cap = body.get("max_tokens")
    if cap is None:
        cap = DEFAULT_MAX_TOKENS
    if not isinstance(cap, int) or isinstance(cap, bool) or cap < 1:
        raise BadRequest("max_completion_tokens/max_tokens는 1 이상의 정수여야 한다")
    if body.get("ignore_eos"):
        return cap, cap
    low = math.ceil(cap / 2)
    rng = _request_rng(seed, req_hash, "len")
    return cap, low + math.floor(rng.random() * (cap - low + 1))


def should_fail(config: MockConfig, req_hash: str) -> bool:
    return config.error_rate > 0 and _request_rng(config.seed, req_hash, "err").random() < (
        config.error_rate
    )


def _parse_messages(body: Any) -> list[Message]:
    if not isinstance(body, dict):
        raise BadRequest("요청 본문은 JSON 객체여야 한다")
    messages = body.get("messages")
    if (
        not isinstance(messages, list)
        or not messages
        or not all(isinstance(m, dict) for m in messages)
    ):
        raise BadRequest("messages는 비어 있지 않은 메시지 객체 목록이어야 한다")
    for message in messages:
        content = message.get("content")
        if isinstance(content, list) and any(
            isinstance(part, dict)
            and part.get("type") == "text"
            and not isinstance(part.get("text"), str)
            for part in content
        ):
            raise BadRequest("text 파트의 text는 문자열이어야 한다")
    if not isinstance(body.get("stream_options"), dict | None):
        raise BadRequest("stream_options는 객체여야 한다")
    return messages


def _error(status: int, message: str, kind: str) -> web.Response:
    return web.json_response(
        {"error": {"message": message, "type": kind, "code": status}}, status=status
    )


def _usage(prompt: int, completion: int, cached: int) -> dict[str, Any]:
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "prompt_tokens_details": {"cached_tokens": cached},
    }


@dataclass(frozen=True)
class _Reply:
    ident: str
    model: str
    tokens: int
    cap: int
    usage: dict[str, Any] | None
    ttft_s: float

    @property
    def finish_reason(self) -> str:
        return "length" if self.tokens == self.cap else "stop"


def _chunk(reply: _Reply, created: int, delta: dict[str, str], finish: str | None) -> bytes:
    payload = {
        "id": reply.ident,
        "object": "chat.completion.chunk",
        "created": created,
        "model": reply.model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n".encode()


async def _sleep_until(deadline: float) -> None:
    delay = deadline - asyncio.get_running_loop().time()
    if delay > 0:
        await asyncio.sleep(delay)


async def _stream(
    request: web.Request, state: _State, reply: _Reply, start: float, include_usage: bool
) -> web.StreamResponse:
    resp = web.StreamResponse(
        headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"}
    )
    await resp.prepare(request)
    created = int(time.time())
    deadline = start + reply.ttft_s
    interval = 1 / state.config.tps
    for i in range(reply.tokens):
        await _sleep_until(deadline)
        delta = {"content": PIECES[i % len(PIECES)]}
        if i == 0:
            delta = {"role": "assistant", **delta}
        last = i == reply.tokens - 1
        await resp.write(_chunk(reply, created, delta, reply.finish_reason if last else None))
        deadline += state.jittered(interval)
    if include_usage and reply.usage is not None:
        tail = {
            "id": reply.ident,
            "object": "chat.completion.chunk",
            "created": created,
            "model": reply.model,
            "choices": [],
            "usage": reply.usage,
        }
        await resp.write(f"data: {json.dumps(tail, separators=(',', ':'))}\n\n".encode())
    await resp.write(b"data: [DONE]\n\n")
    await resp.write_eof()
    return resp


async def _complete(state: _State, reply: _Reply, start: float) -> web.Response:
    deadline = start + reply.ttft_s
    for _ in range(reply.tokens - 1):
        deadline += state.jittered(1 / state.config.tps)
    await _sleep_until(deadline)
    text = "".join(PIECES[i % len(PIECES)] for i in range(reply.tokens))
    body: dict[str, Any] = {
        "id": reply.ident,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": reply.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": reply.finish_reason,
            }
        ],
    }
    if reply.usage is not None:
        body["usage"] = reply.usage
    return web.json_response(body)


async def chat_completions(request: web.Request) -> web.StreamResponse:
    state = request.app[STATE]
    config = state.config
    start = asyncio.get_running_loop().time()
    try:
        body = await request.json()
        messages = _parse_messages(body)
        req_hash = request_hash(messages)
        cap, tokens = output_budget(body, config.seed, req_hash)
    except (ValueError, RecursionError) as exc:  # RecursionError: absurdly nested JSON
        return _error(
            400,
            str(exc) if isinstance(exc, BadRequest) else "JSON 파싱 실패",
            "invalid_request_error",
        )
    if should_fail(config, req_hash):
        return _error(500, "simulated error", "server_error")

    digests = prefix_digests(messages)
    cached = state.cache.cached_tokens(messages, digests)
    prompt = sum(message_tokens(m) for m in messages)
    ttft_ms = config.ttft_ms * (config.cache_ttft_factor if cached > 0 else 1.0)
    reply = _Reply(
        ident=f"chatcmpl-mock-{next(state.ids)}",
        model=str(body.get("model") or MODEL_ID),
        tokens=tokens,
        cap=cap,
        usage=_usage(prompt, tokens, cached) if config.usage else None,
        ttft_s=state.jittered(ttft_ms) / 1000,
    )
    if body.get("stream"):
        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
        resp = await _stream(request, state, reply, start, include_usage)
    else:
        resp = await _complete(state, reply, start)
    # Reached only after a complete reply: errors and client disconnects register nothing.
    state.cache.register(digests)
    return resp


async def models(request: web.Request) -> web.Response:
    return web.json_response({"object": "list", "data": [{"id": MODEL_ID, "object": "model"}]})


async def health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


def create_app(config: MockConfig) -> web.Application:
    app = web.Application(client_max_size=MAX_BODY_BYTES)
    app[STATE] = _State(config)
    app.router.add_post("/v1/chat/completions", chat_completions)
    app.router.add_get("/v1/models", models)
    app.router.add_get("/health", health)
    return app


def parse_listen(value: str) -> tuple[str, int]:
    host, sep, port = value.rpartition(":")
    if not sep or not host or not port.isdigit():
        raise ValueError(
            "--listen은 host:port 형식이다 (예: 127.0.0.1:8000, 모든 인터페이스는 0.0.0.0:8000)"
        )
    return host.strip("[]"), int(port)


def serve(config: MockConfig) -> None:
    web.run_app(
        create_app(config),
        host=config.host,
        port=config.port,
        access_log=None,
        # aiohttp calls print only after every socket is bound
        print=lambda _: log.info("mock server: http://%s:%d %s", config.host, config.port, config),
        # cancel the handler on client disconnect: no prefix registration, no traceback spam,
        # and Ctrl+C/SIGTERM do not wait out in-flight streams
        handler_cancellation=True,
        shutdown_timeout=SHUTDOWN_TIMEOUT_S,
    )
