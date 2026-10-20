from __future__ import annotations

import hashlib
import json
import random

import pytest
from conftest import ARRIVAL_LOAD, make_dict

from personaload.config import scenario_from_dict
from personaload.workload.dataset import dumps_line, session_record, write_dataset
from personaload.workload.estimate import dist_means, estimate_stage
from personaload.workload.personas import (
    PersonaError,
    file_personas,
    load_personas,
    synthetic_personas,
)
from personaload.workload.sessions import (
    WorkloadError,
    _session_id,
    arrival_offsets_ms,
    build_stage_sessions,
    concurrency_session_count,
    session_think_ms,
)
from personaload.workload.text import CHARS_PER_TOKEN, approx_tokens, make_text


def _scenario(**overrides):
    return scenario_from_dict(make_dict(**overrides))


# --- text / personas -------------------------------------------------------------------


@pytest.mark.parametrize("language", ["en", "ko"])
@pytest.mark.parametrize("tokens", [1, 5, 50, 300])
def test_make_text_hits_token_target(language, tokens):
    import random

    text = make_text(random.Random(tokens), tokens, language)
    assert text[-1] in ".?!"
    got = approx_tokens(text, language)
    assert tokens <= got <= tokens + 12


@pytest.mark.parametrize("language", ["en", "ko"])
def test_synthetic_personas_are_unique_and_sized(language):
    s = _scenario(session={"language": language})
    personas = synthetic_personas(s.personas, s.seed, language)
    assert len(personas) == 8
    headers = [p.text.split("]")[0] for p in personas]
    assert len(set(headers)) == len(headers)
    for i, p in enumerate(personas):
        assert p.text.startswith(f"[Persona P{i + 1:04d} · {p.name}]")
        tokens = len(p.text) / CHARS_PER_TOKEN[language]
        assert 200 <= tokens <= 400 + 12


def test_personas_do_not_share_long_prefixes():
    s = _scenario()
    texts = [p.text for p in synthetic_personas(s.personas, s.seed, "en")]
    for a in texts:
        for b in texts:
            if a is not b:
                common = next((i for i, (x, y) in enumerate(zip(a, b, strict=False)) if x != y), 0)
                assert common <= len("[Persona P000")  # diverge inside the id at the latest


def test_file_personas(tmp_path):
    path = tmp_path / "p.jsonl"
    rows = [
        {"name": "A", "system_prompt": "You are A."},
        {"name": "B", "system_prompt": "  "},
        {"name": "C", "system_prompt": "You are C."},
        {"name": "D", "system_prompt": "You are D."},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n\n", encoding="utf-8")
    got = file_personas(path, None)
    assert [p.name for p in got] == ["A", "C", "D"]
    assert [p.index for p in got] == [0, 1, 2]
    assert [p.name for p in file_personas(path, 2)] == ["A", "C"]


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("{bad json\n", "JSON"),
        ("[1, 2]\n", "object"),
        ('{"name": "x", "system_prompt": ""}\n', "non-empty"),
    ],
)
def test_file_personas_errors(tmp_path, content, message):
    path = tmp_path / "p.jsonl"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(PersonaError, match=message):
        file_personas(path, None)


def test_file_personas_missing_file(tmp_path):
    with pytest.raises(PersonaError, match="cannot read"):
        file_personas(tmp_path / "none.jsonl", None)


def test_load_personas_dispatch(tmp_path):
    path = tmp_path / "p.jsonl"
    path.write_text('{"name": "A", "system_prompt": "hi"}\n', encoding="utf-8")
    s = _scenario(
        personas={"source": "file", "file": str(path), "count": None, "system_prompt_tokens": None}
    )
    assert [p.text for p in load_personas(s.personas, s.seed, "en")] == ["hi"]


# --- sessions --------------------------------------------------------------------------


def test_concurrency_session_count_formula():
    s = _scenario(
        session={
            "turns": {"dist": "constant", "value": 3},
            "think_time": {"dist": "constant", "value": "2s"},
        },
    )
    # L = (3-1)*2s = 4s; n = ceil(3 * 4 * 25s / 4s) + 4 = 79
    assert session_think_ms(s) == 4000
    assert concurrency_session_count(s, s.load.stages[0]) == 79


def test_stage_sessions_override():
    s = _scenario(load={"stages": [{"concurrency": 4, "duration": "20s", "sessions": 7}]})
    assert concurrency_session_count(s, s.load.stages[0]) == 7
    assert len(build_stage_sessions(s, 8, 1)) == 7


def test_session_count_rejects_tiny_think_time():
    s = _scenario(session={"turns": {"dist": "constant", "value": 1}})
    with pytest.raises(WorkloadError, match="L="):
        concurrency_session_count(s, s.load.stages[0])


def test_arrival_offsets_are_poisson_like():
    s = _scenario(load=ARRIVAL_LOAD)
    offsets = arrival_offsets_ms(s.seed, 1, s.load.stages[0])
    assert offsets[0] == 0.0
    assert offsets == sorted(offsets)
    assert offsets[-1] < 12_000
    assert 10 <= len(offsets) <= 45  # λ=2/s over 12s → mean ≈ 25
    assert all(round(o, 1) == o for o in offsets)
    assert offsets == arrival_offsets_ms(s.seed, 1, s.load.stages[0])
    assert offsets != arrival_offsets_ms(s.seed, 2, s.load.stages[0])


def test_session_contents_follow_spec():
    s = _scenario()
    plans = build_stage_sessions(s, 8, 1)
    assert len({p.session_id for p in plans}) == len(plans)
    assert plans[0].session_id == "s01-000001"
    for plan in plans:
        assert 0 <= plan.persona_index < 8
        assert 2 <= len(plan.turns) <= 5
        assert plan.turns[0].delay_ms is None
        assert all(t.timestamp_ms is None for t in plan.turns)
        assert all(20 <= t.output_length <= 60 for t in plan.turns)
        assert all(100 <= t.delay_ms <= 10_000 for t in plan.turns[1:])
        assert all(round(t.delay_ms, 1) == t.delay_ms for t in plan.turns[1:])


def test_session_j_depends_only_on_seed_stage_and_index():
    few = build_stage_sessions(
        _scenario(load={"stages": [{"concurrency": 1, "duration": "1s", "sessions": 5}]}), 8, 1
    )
    many = build_stage_sessions(
        _scenario(load={"stages": [{"concurrency": 1, "duration": "1s", "sessions": 10}]}), 8, 1
    )
    assert many[:5] == few


def test_arrival_sessions_carry_start_on_first_turn_only():
    s = _scenario(load=ARRIVAL_LOAD)
    plans = build_stage_sessions(s, 8, 1)
    offsets = arrival_offsets_ms(s.seed, 1, s.load.stages[0])
    assert [p.start_ms for p in plans] == offsets
    assert all(t.timestamp_ms is None for p in plans for t in p.turns[1:])


def test_session_id_width_grows():
    assert _session_id(3, 42, 6) == "s03-000042"
    assert _session_id(12, 1_234_567, 7) == "s12-1234567"


# --- dataset lines ---------------------------------------------------------------------


def _lines(s, stage_no=1):
    personas = load_personas(s.personas, s.seed, s.session.language)
    plans = build_stage_sessions(s, len(personas), stage_no)
    return [session_record(p, personas[p.persona_index]) for p in plans], plans


def test_record_layout_concurrency():
    records, plans = _lines(_scenario())
    for record, plan in zip(records, plans, strict=True):
        assert list(record) == ["session_id", "turns"]
        system, *users = record["turns"]
        assert system == {"role": "system", "text": system["text"]}
        assert system["text"].startswith("[Persona P")
        assert len(users) == len(plan.turns)
        assert list(users[0]) == ["text", "output_length"]
        for turn in users[1:]:
            assert list(turn) == ["text", "delay", "output_length"]


def test_record_layout_arrival_has_top_level_timestamp():
    records, _ = _lines(_scenario(load=ARRIVAL_LOAD))
    assert records[0]["timestamp"] == 0.0
    for record in records:
        assert list(record) == ["session_id", "timestamp", "turns"]
        first_user = record["turns"][1]
        assert list(first_user) == ["text", "timestamp", "output_length"]
        assert first_user["timestamp"] == record["timestamp"]


def test_dumps_line_keeps_korean_and_is_compact():
    records, _ = _lines(_scenario(session={"language": "ko"}))
    line = dumps_line(records[0])
    assert "\\u" not in line
    assert ", " not in line.split('"text"')[0]
    assert any("가" <= ch <= "힣" for ch in line)


def test_write_dataset_returns_sha256(tmp_path):
    s = _scenario()
    personas = load_personas(s.personas, s.seed, "en")
    plans = build_stage_sessions(s, len(personas), 1)
    path = tmp_path / "stage_01" / "dataset.jsonl"
    digest = write_dataset(path, plans, personas)
    data = path.read_bytes()
    assert digest == hashlib.sha256(data).hexdigest()
    assert data.endswith(b"\n")
    assert data.count(b"\n") == len(plans)


def test_file_personas_keep_unicode_line_separators(tmp_path):
    path = tmp_path / "p.jsonl"
    prompt = "line one line two\u0085end"
    path.write_text(
        json.dumps({"name": "A", "system_prompt": prompt}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    assert file_personas(path, None)[0].text == prompt


def test_file_personas_reject_lone_surrogates(tmp_path):
    path = tmp_path / "p.jsonl"
    path.write_text('{"name": "S", "system_prompt": "bad \\ud800 x"}\n', encoding="utf-8")
    with pytest.raises(PersonaError, match="surrogates"):
        file_personas(path, None)


def test_file_personas_reject_non_utf8(tmp_path):
    path = tmp_path / "p.jsonl"
    path.write_bytes(b'{"name": "A", "system_prompt": "\xff\xfe"}\n')
    with pytest.raises(PersonaError, match="cannot read"):
        file_personas(path, None)


def test_seed_keys_have_the_spec_forms(monkeypatch):
    import personaload.workload.personas as personas_mod
    import personaload.workload.sessions as sessions_mod
    from personaload.workload import rng as rng_mod

    keys = []

    def spy(seed, *parts):
        keys.append(":".join(str(p) for p in (seed, *parts)))
        return rng_mod.child_rng(seed, *parts)

    monkeypatch.setattr(personas_mod, "child_rng", spy)
    monkeypatch.setattr(sessions_mod, "child_rng", spy)
    s = _scenario(load=ARRIVAL_LOAD)
    build_stage_sessions(s, len(load_personas(s.personas, s.seed, "en")), 1)
    assert keys[:2] == ["42:persona:0", "42:persona:1"]
    assert "42:arrival:1" in keys
    assert "42:session:1:1" in keys and "42:session:1:0" not in keys


def test_session_id_width_grows_past_a_million(monkeypatch):
    import personaload.workload.sessions as sessions_mod

    monkeypatch.setattr(sessions_mod, "concurrency_session_count", lambda *_: 1_000_000)
    monkeypatch.setattr(
        sessions_mod,
        "_build_session",
        lambda _s, _p, stage_no, index, width, _start: sessions_mod._session_id(
            stage_no, index, width
        ),
    )
    ids = build_stage_sessions(_scenario(), 8, 1)
    assert ids[0] == "s01-0000001" and ids[-1] == "s01-1000000"


GOLDEN_SHA256 = {
    "en": "b32a1271dd928a2ca550a22c4fa1108a8932f4b77ccb883e67f2777bbd9492c0",
    "ko": "f2a837d08f52f9c4bef0b2bcc41c38dbc81b3741c51be1b42db8d1d33b5ef65b",
}


@pytest.mark.parametrize("language", ["en", "ko"])
def test_golden_dataset_bytes(tmp_path, language):
    """Pins the full generator output. Update only for an intended format/algorithm change."""
    s = _scenario(
        session={"language": language},
        load={"stages": [{"concurrency": 2, "duration": "10s", "sessions": 4}]},
    )
    personas = load_personas(s.personas, s.seed, language)
    digest = write_dataset(
        tmp_path / "d.jsonl", build_stage_sessions(s, len(personas), 1), personas
    )
    assert digest == GOLDEN_SHA256[language]


# --- RAG: retrieved documents per user turn ---------------------------------------------------


RAG = {"retrieved_tokens": {"dist": "constant", "value": 200}}


@pytest.mark.parametrize(("language", "doc", "question"), [
    ("ko", "[참고 문서]\n", "\n\n[질문]\n"),
    ("en", "[Retrieved documents]\n", "\n\n[Question]\n"),
])  # fmt: skip
def test_rag_prepends_a_fresh_document_to_every_user_turn(language, doc, question):
    plain = build_stage_sessions(_scenario(session={"language": language}), 8, 1)
    rag = build_stage_sessions(_scenario(session={"language": language} | RAG), 8, 1)
    docs = []
    for p, r in zip(plain, rag, strict=True):
        assert r.persona_index == p.persona_index and len(r.turns) == len(p.turns)
        for pt, rt in zip(p.turns, r.turns, strict=True):
            assert (rt.output_length, rt.delay_ms) == (pt.output_length, pt.delay_ms)
            head, _, rest = rt.text.partition(question)
            assert head.startswith(doc) and rest == pt.text  # question unchanged, document in front
            body = head.removeprefix(doc)
            assert 200 <= approx_tokens(body, language) <= 212
            docs.append(body)
    assert len(set(docs)) == len(docs)  # a new retrieval every turn


def test_rag_estimate_counts_the_documents():
    plain, rag = _scenario(), _scenario(session=RAG)
    assert "session.retrieved_tokens" in [m.name for m in dist_means(rag)]
    personas = load_personas(rag.personas, rag.seed, "en")
    a, b = estimate_stage(plain, personas, 1), estimate_stage(rag, personas, 1)
    assert a.sessions == b.sessions
    # every request carries one 200-token document: E[turns] 3.5 (uniform 2..5) x en bytes
    sample = make_text(random.Random(0), 2000, "en")
    doc_bytes = 200 * CHARS_PER_TOKEN["en"] * len(sample.encode()) / len(sample)
    assert abs(b.inputs_json_bytes - a.inputs_json_bytes - a.sessions * 3.5 * doc_bytes) <= 1


RAG_GOLDEN_SHA256 = "5cbbabb6b76c0d0f59d010a87187fe145e470507e6a2bb9b6a761970ea49dd71"


def test_rag_golden_dataset_bytes(tmp_path):
    """Pins the retrieval stream key and the document format. Update only for an intended change."""
    s = _scenario(
        session={"language": "ko"} | RAG,
        load={"stages": [{"concurrency": 2, "duration": "10s", "sessions": 4}]},
    )
    personas = load_personas(s.personas, s.seed, "ko")
    digest = write_dataset(
        tmp_path / "d.jsonl", build_stage_sessions(s, len(personas), 1), personas
    )
    assert digest == RAG_GOLDEN_SHA256
