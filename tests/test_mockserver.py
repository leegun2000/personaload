from __future__ import annotations

import asyncio
import json
import math
import re
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from personaload.mockserver.app import (
    DEFAULT_MAX_TOKENS,
    STATE,
    MockConfig,
    create_app,
    output_budget,
    parse_listen,
    request_hash,
)
from personaload.mockserver.cache import PrefixCache, message_text, message_tokens, prefix_digests

FAST = {"ttft_ms": 0.0, "tps": 10_000.0}
SYSTEM = {"role": "system", "content": "You are Mira, a retired navigator." * 3}


def _messages(*user: str) -> list[dict]:
    out = [SYSTEM]
    for i, text in enumerate(user):
        if i:
            out.append({"role": "assistant", "content": f"reply {i}"})
        out.append({"role": "user", "content": text})
    return out


def _run(config: MockConfig, scenario):
    async def main():
        app = create_app(config)
        async with TestClient(TestServer(app)) as client:
            return await scenario(client, app)

    return asyncio.run(main())


async def _post(client, **body):
    body.setdefault("model", "m")
    return await client.post("/v1/chat/completions", json=body)


def _sse(text: str) -> list:
    events = []
    for block in text.split("\n\n"):
        if block.startswith("data: "):
            data = block[len("data: ") :]
            events.append(data if data == "[DONE]" else json.loads(data))
    return events


# --- cache / token helpers ------------------------------------------------------------


def test_message_text_and_tokens():
    assert message_text({"content": "abcd"}) == "abcd"
    parts = [{"type": "text", "text": "ab"}, {"type": "image_url"}, {"type": "text", "text": "cde"}]
    assert message_text({"content": parts}) == "abcde"
    assert message_text({"content": None}) == ""
    assert message_tokens({"content": "abcde"}) == 2
    assert message_tokens({"content": ""}) == 0


def test_prefix_digests_are_nested_and_ignore_extra_keys():
    a = prefix_digests([{"role": "system", "content": "x"}, {"role": "user", "content": "y"}])
    b = prefix_digests([{"role": "system", "content": "x", "name": "n"}])
    assert a[0] == b[0] and len(a) == 2 and a[0] != a[1]
    parts = prefix_digests([{"role": "system", "content": [{"type": "text", "text": "x"}]}])
    assert parts[0] == a[0]


def test_prefix_cache_longest_hit():
    cache = PrefixCache()
    first = _messages("hello")
    cache.register(prefix_digests(first))
    follow_up = _messages("hello", "again")
    expected = sum(message_tokens(m) for m in first)
    assert cache.cached_tokens(follow_up, prefix_digests(follow_up)) == expected
    other = [{"role": "system", "content": "different persona"}]
    assert cache.cached_tokens(other, prefix_digests(other)) == 0


# --- config / helpers ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"jitter": 1.0}, "jitter"),
        ({"jitter": -0.1}, "jitter"),
        ({"tps": 0}, "tps"),
        ({"ttft_ms": -1}, "ttft"),
        ({"cache_ttft_factor": -0.5}, "cache-ttft-factor"),
        ({"error_rate": 30}, re.escape("비율(0~1)")),
        ({"error_rate": -0.1}, "error-rate"),
        ({"port": 0}, "port"),
    ],
)
def test_config_validation(kwargs, message):
    with pytest.raises(ValueError, match=message):
        MockConfig(**kwargs)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("127.0.0.1:8000", ("127.0.0.1", 8000)),
        ("0.0.0.0:9", ("0.0.0.0", 9)),
        ("[::1]:80", ("::1", 80)),
    ],
)
def test_parse_listen(value, expected):
    assert parse_listen(value) == expected


@pytest.mark.parametrize("bad", [":8000", "8000", "host:", "host:abc"])
def test_parse_listen_rejects(bad):
    with pytest.raises(ValueError, match="host:port"):
        parse_listen(bad)


def test_output_budget_rules():
    h = request_hash(_messages("x"))
    assert output_budget({"ignore_eos": True, "max_completion_tokens": 40}, 0, h) == (40, 40)
    assert output_budget({"max_tokens": 30}, 0, h)[0] == 30
    assert output_budget({}, 0, h)[0] == DEFAULT_MAX_TOKENS
    lengths = {
        output_budget({"max_completion_tokens": 20}, 0, request_hash(_messages(str(i))))[1]
        for i in range(300)
    }
    assert lengths == set(range(10, 21))
    # deterministic per (seed, request)
    assert output_budget({"max_tokens": 99}, 5, h) == output_budget({"max_tokens": 99}, 5, h)


# --- HTTP behaviour ----------------------------------------------------------------------


def test_non_streaming_response_format():
    async def scenario(client, app):
        resp = await _post(
            client, messages=_messages("hi"), max_completion_tokens=12, ignore_eos=True
        )
        return resp.status, await resp.json()

    status, body = _run(MockConfig(**FAST), scenario)
    assert status == 200
    assert body["object"] == "chat.completion" and body["model"] == "m"
    choice = body["choices"][0]
    assert choice["message"]["role"] == "assistant"
    assert choice["finish_reason"] == "length"
    assert len(choice["message"]["content"]) == 12 * 4
    prompt = sum(message_tokens(m) for m in _messages("hi"))
    assert body["usage"] == {
        "prompt_tokens": prompt,
        "completion_tokens": 12,
        "total_tokens": prompt + 12,
        "prompt_tokens_details": {"cached_tokens": 0},
    }


@pytest.mark.parametrize("include_usage", [True, False])
def test_streaming_response_format(include_usage):
    async def scenario(client, app):
        resp = await _post(
            client,
            messages=_messages("hi"),
            stream=True,
            stream_options={"include_usage": include_usage},
            max_completion_tokens=5,
            ignore_eos=True,
        )
        return resp.status, resp.headers["Content-Type"], await resp.text()

    status, ctype, text = _run(MockConfig(**FAST), scenario)
    assert status == 200 and ctype.startswith("text/event-stream")
    events = _sse(text)
    assert events[-1] == "[DONE]"
    chunks = [e for e in events[:-1] if e["choices"]]
    assert len(chunks) == 5
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert all(c["object"] == "chat.completion.chunk" for c in chunks)
    assert [c["choices"][0]["finish_reason"] for c in chunks] == [None] * 4 + ["length"]
    usage = [e for e in events[:-1] if not e["choices"]]
    if include_usage:
        assert len(usage) == 1 and usage[0]["usage"]["completion_tokens"] == 5
    else:
        assert usage == []


def test_no_usage_mode_omits_usage_everywhere():
    async def scenario(client, app):
        streamed = await (
            await _post(
                client, messages=_messages("a"), stream=True, stream_options={"include_usage": True}
            )
        ).text()
        plain = await (await _post(client, messages=_messages("b"))).json()
        return streamed, plain

    streamed, plain = _run(MockConfig(usage=False, **FAST), scenario)
    assert all(e == "[DONE]" or e["choices"] for e in _sse(streamed))
    assert "usage" not in plain


def test_stop_finish_reason_when_shorter_than_cap():
    async def scenario(client, app):
        return [
            await (await _post(client, messages=_messages(str(i)), max_tokens=10)).json()
            for i in range(20)
        ]

    bodies = _run(MockConfig(**FAST), scenario)
    for body in bodies:
        n = body["usage"]["completion_tokens"]
        assert 5 <= n <= 10
        assert body["choices"][0]["finish_reason"] == ("length" if n == 10 else "stop")


def test_revisit_hits_prefix_cache_with_history():
    async def scenario(client, app):
        first = await (await _post(client, messages=_messages("hello"))).json()
        again = await (await _post(client, messages=_messages("hello"))).json()
        follow = await (await _post(client, messages=_messages("hello", "more"))).json()
        return first, again, follow

    first, again, follow = _run(MockConfig(**FAST), scenario)
    assert first["usage"]["prompt_tokens_details"]["cached_tokens"] == 0
    assert (
        again["usage"]["prompt_tokens_details"]["cached_tokens"] == again["usage"]["prompt_tokens"]
    )
    first_prompt = sum(message_tokens(m) for m in _messages("hello"))
    assert follow["usage"]["prompt_tokens_details"]["cached_tokens"] == first_prompt


async def _ttft(client, messages) -> float:
    start = time.perf_counter()
    resp = await _post(client, messages=messages, stream=True, max_completion_tokens=2)
    async for line in resp.content:
        if line.startswith(b"data: {"):
            elapsed = time.perf_counter() - start
            await resp.read()
            return elapsed
    raise AssertionError("no chunk")


def test_cache_hit_shortens_ttft():
    async def scenario(client, app):
        cold = await _ttft(client, _messages("persona check"))
        warm = await _ttft(client, _messages("persona check"))
        return cold, warm

    cold, warm = _run(MockConfig(ttft_ms=300, tps=1000, cache_ttft_factor=0.2), scenario)
    assert 0.28 <= cold < 0.6
    assert warm < 0.15


def test_default_factor_keeps_ttft_on_cache_hit():
    async def scenario(client, app):
        await _ttft(client, _messages("same"))
        return await _ttft(client, _messages("same"))

    warm = _run(MockConfig(ttft_ms=200, tps=1000), scenario)
    assert warm >= 0.19


def test_token_pacing_follows_tps():
    async def scenario(client, app):
        start = time.perf_counter()
        resp = await _post(
            client,
            messages=_messages("pace"),
            stream=True,
            max_completion_tokens=21,
            ignore_eos=True,
        )
        await resp.read()
        return time.perf_counter() - start

    elapsed = _run(MockConfig(ttft_ms=50, tps=100), scenario)
    expected = 0.05 + 20 / 100  # first token at ttft, then 20 intervals
    assert expected * 0.9 <= elapsed <= expected * 1.4


def test_jitter_stays_within_bounds():
    async def scenario(client, app):
        start = time.perf_counter()
        await (await _post(client, messages=_messages("j"), max_completion_tokens=1)).json()
        return time.perf_counter() - start

    elapsed = _run(MockConfig(ttft_ms=200, tps=1000, jitter=0.5), scenario)
    assert 0.1 * 0.95 <= elapsed <= 0.3 * 1.3


@pytest.mark.parametrize("rate", [0.0, 1.0])
def test_error_rate_extremes(rate):
    async def scenario(client, app):
        statuses = [(await _post(client, messages=_messages(str(i)))).status for i in range(10)]
        return statuses, len(app[STATE].cache)

    statuses, cached = _run(MockConfig(error_rate=rate, **FAST), scenario)
    assert statuses == ([500] * 10 if rate else [200] * 10)
    assert (cached == 0) == bool(rate)  # failed requests register no prefixes


def test_error_rate_fraction_and_body():
    async def scenario(client, app):
        resps = [await _post(client, messages=_messages(str(i))) for i in range(300)]
        failed = [r for r in resps if r.status == 500]
        return len(failed) / len(resps), await failed[0].json()

    fraction, body = _run(MockConfig(error_rate=0.3, **FAST), scenario)
    assert 0.2 <= fraction <= 0.4
    assert body["error"]["code"] == 500


@pytest.mark.parametrize(
    "body",
    [
        {"messages": []},
        {"messages": "hi"},
        {"messages": ["hi"]},
        {"messages": [{"role": "user", "content": "x"}], "max_tokens": 0},
        {"messages": [{"role": "user", "content": "x"}], "max_tokens": True},
    ],
)
def test_bad_requests(body):
    async def scenario(client, app):
        resp = await client.post("/v1/chat/completions", json=body)
        return resp.status, await resp.json()

    status, payload = _run(MockConfig(**FAST), scenario)
    assert status == 400 and payload["error"]["type"] == "invalid_request_error"


def test_invalid_json_and_non_object():
    async def scenario(client, app):
        a = await client.post("/v1/chat/completions", data=b"{not json")
        b = await client.post("/v1/chat/completions", json=[1, 2])
        return a.status, b.status

    assert _run(MockConfig(**FAST), scenario) == (400, 400)


def test_models_and_health():
    async def scenario(client, app):
        return await (await client.get("/v1/models")).json(), await (
            await client.get("/health")
        ).json()

    models, health = _run(MockConfig(**FAST), scenario)
    assert models["data"][0]["id"] == "mock-model"
    assert health == {"status": "ok"}


def test_ceil_half_budget_for_odd_caps():
    h = request_hash(_messages("odd"))
    cap, n = output_budget({"max_tokens": 7}, 3, h)
    assert cap == 7 and math.ceil(7 / 2) <= n <= 7


# --- B2 review regressions ---------------------------------------------------------------


def test_same_system_prompt_new_session_hits_system_prefix():
    async def scenario(client, app):
        await (await _post(client, messages=_messages("first session"))).json()
        return await (await _post(client, messages=_messages("second session"))).json()

    body = _run(MockConfig(**FAST), scenario)
    assert body["usage"]["prompt_tokens_details"]["cached_tokens"] == message_tokens(SYSTEM)


def test_request_hash_and_seed_derivation_follow_spec_formula():
    import hashlib
    import random

    from personaload.mockserver.app import _request_rng, should_fail

    messages = [{"role": "user", "content": "안녕", "b": 1, "a": 2}]
    canonical = json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    h = hashlib.sha256(canonical.encode()).hexdigest()
    assert request_hash(messages) == h
    for purpose in ("len", "err"):
        expected = random.Random(
            int.from_bytes(hashlib.sha256(f"7:{h}:{purpose}".encode()).digest()[:8], "big")
        ).random()
        assert _request_rng(7, h, purpose).random() == expected
    u_err = _request_rng(7, h, "err").random()
    assert should_fail(MockConfig(error_rate=u_err + 1e-9, seed=7), h)
    assert not should_fail(MockConfig(error_rate=u_err, seed=7), h)


def test_cap_precedence():
    h = request_hash(_messages("cap"))
    both = {"max_completion_tokens": 5, "max_tokens": 50, "ignore_eos": True}
    assert output_budget(both, 0, h) == (5, 5)
    assert output_budget({"max_tokens": 50, "ignore_eos": True}, 0, h) == (50, 50)


def _recorded_deadlines(config: MockConfig, tokens: int, monkeypatch) -> list[float]:
    import personaload.mockserver.app as app_mod

    deadlines = []
    original = app_mod._sleep_until

    async def record(deadline):
        deadlines.append(deadline)
        await asyncio.sleep(0.003)  # oversleep: absolute pacing must not accumulate it
        await original(deadline)

    monkeypatch.setattr(app_mod, "_sleep_until", record)

    async def scenario(client, app):
        resp = await _post(
            client,
            messages=_messages("pace"),
            stream=True,
            max_completion_tokens=tokens,
            ignore_eos=True,
        )
        await resp.read()

    _run(config, scenario)
    return deadlines


def test_absolute_deadline_pacing(monkeypatch):
    deadlines = _recorded_deadlines(MockConfig(ttft_ms=10, tps=200), 11, monkeypatch)
    gaps = [b - a for a, b in zip(deadlines, deadlines[1:], strict=False)]
    assert len(gaps) == 10 and all(g == pytest.approx(1 / 200, abs=1e-9) for g in gaps)


def test_inter_token_jitter_bounds(monkeypatch):
    deadlines = _recorded_deadlines(MockConfig(ttft_ms=0, tps=1000, jitter=0.5), 30, monkeypatch)
    gaps = [b - a for a, b in zip(deadlines, deadlines[1:], strict=False)]
    assert all(0.5 / 1000 <= g <= 1.5 / 1000 for g in gaps)
    assert len({round(g, 9) for g in gaps}) > 10  # actually jittered


def test_large_body_is_accepted():
    big = _messages("x" * (2 << 20))  # 2 MiB user message, above aiohttp's 1 MiB default

    async def scenario(client, app):
        resp = await _post(client, messages=big, max_tokens=1)
        return resp.status

    assert _run(MockConfig(**FAST), scenario) == 200


@pytest.mark.parametrize(
    "body",
    [
        {"messages": [{"role": "user", "content": [{"type": "text", "text": 5}]}]},
        {"messages": [{"role": "user", "content": "x"}], "stream_options": "yes"},
    ],
)
def test_wrong_inner_types_are_400(body):
    async def scenario(client, app):
        return (await client.post("/v1/chat/completions", json=body)).status

    assert _run(MockConfig(**FAST), scenario) == 400


def test_absurdly_nested_json_is_400():
    async def scenario(client, app):
        data = b'{"messages": ' + b"[" * 100_000 + b"]" * 100_000 + b"}"
        return (await client.post("/v1/chat/completions", data=data)).status

    assert _run(MockConfig(**FAST), scenario) == 400
