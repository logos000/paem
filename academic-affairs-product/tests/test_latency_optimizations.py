"""Latency optimizations (2026-10-15), each tied to an observed failure:

- streaming answer-field extraction: the raw JSON stream used to render as
  syntax fragments for the whole generation, so users waited for everything;
- stream deadline + inter-chunk read timeout: a slowly dripping connection
  once ran 30 minutes before the template fallback;
- query-optimization LRU + keyword skip: repeat questions re-paid the LLM
  round trip (logs show the same question asked twice at full price);
- max_tokens cap: bounds the output-token tail;
- schema v2: inline [E1] citations extracted locally, so the model no longer
  writes the answer twice (answer + claims restatement ~= 2x output tokens).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from product.answer import ANSWER_SCHEMA_VERSION, _extract_inline_claims, generate_answer
from product.config import RuntimeConfig
from product.contracts import parse_answer_context
from product.providers import (
    FakeProvider,
    _AnswerFieldStream,
    _looks_like_keyword_query,
    _reset_query_cache,
    parse_structured_answer,
    ApiProvider,
    Breaker,
)


# ------------------------------------------------------------------ fixtures

def _runtime_config(**overrides: Any) -> RuntimeConfig:
    base = RuntimeConfig(
        builder_url="http://127.0.0.1:8772", arm="active_llm_validated_ir", top_k=10, pack_limit=8,
        request_timeout=90, llm_base_url="http://fake.test", llm_api_key="k", llm_model="test-model",
        llm_timeout=10, ask_log_path=Path("logs/x.jsonl"),
    )
    return dataclasses.replace(base, **overrides) if overrides else base


def _payload(evidence: list[dict[str, Any]] | None = None) -> Any:
    items = evidence if evidence is not None else [
        {"evidence_id": "d1:1:1", "doc_id": "d1", "title": "Course Guide", "text": "Enrollment starts 8:00 on May 20, 2026. TECH101 carries 3 credits.", "blocking": False,
         "provenance": {"doc_uid": "d1", "block_id": "d1#b1", "source_url": "https://example.test/guide"}},
    ]
    return parse_answer_context({
        "version": 2, "question": "When does enrollment start and how many credits?", "today": None,
        "config": "active_llm_validated_ir", "pack_hash": "ab" * 32,
        "query_ir": {"raw": "q", "language": "en", "terms": [], "academic_years": [], "cohorts": [], "audience": "", "course_codes": []},
        "user_context": {},
        "decision": {"status": "sufficient", "abstain": False, "reason": ""},
        "evidence_pack": {
            "query": "q", "query_ir": {}, "evidence": items, "coverage": 0.5, "conflicts": [],
            "fact_conflicts": [], "usable_evidence_ids": [item["evidence_id"] for item in items],
            "evidence_status": "sufficient", "abstain": False, "abstain_reason": "",
        },
        "provenance": {"build_id": "test-build", "config": "active_llm_validated_ir"},
        "latency_ms": 5.0,
    })


def _sse(content: str) -> bytes:
    return f"data: {json.dumps({'choices': [{'delta': {'content': content}}]})}\n\n".encode()


async def _noop(text: str) -> None:
    pass


def _stream_transport(body: bytes) -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(200, content=body))


_RAW = '{"answer": "毕业需要128学分 [E1]。Enrollment opens May 20 [E2].", "refusal": false, "claims": [{"text": "ignored", "evidence_ids": ["E1"]}], "follow_ups": []}'
_VISIBLE = "毕业需要128学分 [E1]。Enrollment opens May 20 [E2]."


# ------------------------------------------------------------------ extraction

def test_answer_field_stream_whole_and_split():
    assert _AnswerFieldStream().feed(_RAW) == _VISIBLE
    for split in range(len(_RAW) + 1):  # arbitrary chunk boundaries
        extractor = _AnswerFieldStream()
        assert extractor.feed(_RAW[:split]) + extractor.feed(_RAW[split:]) == _VISIBLE


def test_answer_field_stream_char_by_char():
    extractor = _AnswerFieldStream()
    out = [extractor.feed(ch) for ch in _RAW]
    assert "".join(out) == _VISIBLE


def test_answer_field_stream_decodes_escapes_and_swallows_tail():
    raw = '{"answer": "line1\\nquote \\"q\\" \\u4e2d\\u6587", "claims": [{"text": "tail must never leak"}]}'
    assert _AnswerFieldStream().feed(raw) == 'line1\nquote "q" 中文'


def test_answer_field_stream_tolerates_preamble_and_fence():
    raw = 'Here you go:\n```json\n{"answer": "plain text", "refusal": false}\n```'
    assert _AnswerFieldStream().feed(raw) == "plain text"


def test_answer_field_stream_missing_answer_key():
    assert _AnswerFieldStream().feed('{"refusal": true, "answer": "证据中未包含该信息"}') == "证据中未包含该信息"
    assert _AnswerFieldStream().feed('{"refusal": true}') == ""


@pytest.mark.asyncio
async def test_stream_completion_forwards_only_answer_text():
    body = (_sse('{"answer": "TECH101 carries 3') + _sse(' credits [E1]."')
            + _sse(',') + _sse(' "refusal": false}') + b"data: [DONE]\n\n")
    provider = ApiProvider(_runtime_config(), transport=_stream_transport(body))
    seen: list[str] = []

    async def collect(text: str) -> None:
        seen.append(text)

    content = await provider._stream_completion(
        httpx.AsyncClient(transport=_stream_transport(body)), "prompt", {"model": "m"}, collect,
    )
    assert "".join(seen) == "TECH101 carries 3 credits [E1]."  # readable prose only
    assert json.loads(content)["refusal"] is False  # raw accumulation stays parseable


# ------------------------------------------------------------------ deadlines

def _slow_stream_transport(delay: float) -> httpx.MockTransport:
    async def handler(request: httpx.Request) -> httpx.Response:
        async def drip():
            yield _sse('{"answer": "')
            await asyncio.sleep(delay)
            yield _sse('never arrives"}')
            yield b"data: [DONE]\n\n"
        return httpx.Response(200, content=drip())
    return httpx.MockTransport(handler)


# NOTE: the inter-chunk read timeout (llm_stream_idle_seconds) is enforced by
# httpcore on real sockets; MockTransport serves content from an in-process
# async iterator and bypasses that layer, so only the wall-clock deadline is
# directly testable here — the deadline is the backstop that bounds the
# worst case either way.


@pytest.mark.asyncio
async def test_stream_deadline_aborts_hanging_generation():
    config = _runtime_config(llm_stream_total_seconds=0.3)
    provider = ApiProvider(config, breaker=Breaker(), transport=_slow_stream_transport(delay=5.0))
    payload = _payload()
    from product.answer import _evidence_refs
    started = asyncio.get_running_loop().time()
    result = await provider.complete_structured(payload.question, _evidence_refs(payload), conflict=False, on_delta=_noop)
    elapsed = asyncio.get_running_loop().time() - started
    assert result is None  # -> caller falls back to the template
    assert provider.breaker.failures == 1
    assert elapsed < 3.0  # aborted at the deadline, not at the drip


# ------------------------------------------------------------------ cache + skip

def _counting_transport(calls: dict[str, int]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        # SSE-shaped: the optimizer streams (the real gateway hangs on
        # non-streaming requests), so the mock must look like a stream
        content = '["graduation credits minimum", "毕业 学分 要求"]'
        sse = f"data: {json.dumps({'choices': [{'delta': {'content': content}}]})}\n\ndata: [DONE]\n\n"
        return httpx.Response(200, content=sse.encode())
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_optimize_queries_cached_per_normalized_question():
    _reset_query_cache()
    calls = {"n": 0}
    provider = ApiProvider(_runtime_config(), transport=_counting_transport(calls))
    first = await provider.optimize_queries("我毕业需要多少学分")
    second = await provider.optimize_queries("我毕业 需要多少学分")  # whitespace-normalized hit
    assert calls["n"] == 1
    assert first == second and first[0] == "graduation credits minimum"


@pytest.mark.asyncio
async def test_keyword_shaped_questions_skip_the_llm_entirely():
    _reset_query_cache()
    calls = {"n": 0}
    provider = ApiProvider(_runtime_config(), transport=_counting_transport(calls))
    assert await provider.optimize_queries("ece374 instructor email") == ["ece374 instructor email"]
    assert await provider.optimize_queries("ECE 220 syllabus") == ["ECE 220 syllabus"]
    assert calls["n"] == 0
    # interrogatives and CJK still go through the optimizer
    await provider.optimize_queries("when is the deadline")
    await provider.optimize_queries("毕业需要多少学分")
    assert calls["n"] == 2


def test_looks_like_keyword_query_boundaries():
    assert _looks_like_keyword_query("ece374 instructor email")
    assert not _looks_like_keyword_query("ece374的老师是谁")  # CJK needs translation
    assert not _looks_like_keyword_query("when is the deadline")  # interrogative
    assert not _looks_like_keyword_query("")  # empty never skips


# ------------------------------------------------------------------ max_tokens

@pytest.mark.asyncio
async def test_max_tokens_cap_sent_to_provider():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.read()))
        return httpx.Response(200, json={"choices": [{"message": {"content": "irrelevant"}}]})

    provider = ApiProvider(_runtime_config(llm_max_tokens=321), transport=httpx.MockTransport(handler))
    payload = _payload()
    from product.answer import _evidence_refs
    await provider.complete_structured(payload.question, _evidence_refs(payload), conflict=False)
    assert captured.get("max_tokens") == 321


@pytest.mark.asyncio
async def test_extra_body_merged_into_generation_and_optimize():
    _reset_query_cache()
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.read()))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    provider = ApiProvider(_runtime_config(llm_extra_body={"think": False}), transport=httpx.MockTransport(handler))
    payload = _payload()
    from product.answer import _evidence_refs
    await provider.complete_structured(payload.question, _evidence_refs(payload), conflict=False)
    await provider.optimize_queries("毕业需要多少学分")
    assert all(body.get("think") is False for body in captured)


def test_extra_body_config_validation(monkeypatch):
    from product.config import ConfigError, RuntimeConfig, load_config

    monkeypatch.setenv("LLM_EXTRA_BODY", '{"think": false}')
    config = load_config()
    assert config.llm_extra_body == {"think": False}
    monkeypatch.setenv("LLM_EXTRA_BODY", "not json")
    with pytest.raises(ConfigError):
        load_config()
    monkeypatch.setenv("LLM_EXTRA_BODY", '["array"]')
    with pytest.raises(ConfigError):
        load_config()


# ------------------------------------------------------------------ schema v2

def test_parse_structured_answer_accepts_missing_claims():
    parsed = parse_structured_answer('{"answer": "毕业需要128学分 [E1]。", "refusal": false}')
    assert parsed is not None and parsed["claims"] == [] and parsed["answer"].endswith("[E1]。")
    # v1 shape still parses
    legacy = parse_structured_answer('{"answer": "ok", "claims": [{"text": "ok", "evidence_ids": ["E1"]}], "refusal": false}')
    assert legacy is not None and legacy["claims"][0]["evidence_ids"] == ["E1"]
    # malformed claims still fail the parse (repair retry fires)
    assert parse_structured_answer('{"answer": "ok", "claims": [{"text": 42}]}') is None


def test_extract_inline_claims_sentences_markers_and_dedupe():
    mapping = {"E1": "d1:1:1", "E2": "d1:1:2"}
    answer = "毕业需要128学分 [E1]。Enrollment opens May 20 [E2]. 无标记的连接句不进claims。\nTECH101 carries 3 credits [E1][E1]."
    claims = _extract_inline_claims(answer, mapping)
    assert [c["text"] for c in claims] == ["毕业需要128学分", "Enrollment opens May 20", "TECH101 carries 3 credits"]
    assert claims[0]["evidence_ids"] == ["d1:1:1"]
    assert claims[2]["evidence_ids"] == ["d1:1:1"]  # deduped
    unknown = _extract_inline_claims("odd cite [E9].", mapping)
    assert unknown[0]["evidence_ids"] == ["E9"]  # survives -> validator flags it


def test_extract_inline_claims_ignores_course_codes():
    assert _extract_inline_claims("ECE374 meets weekly.", {}) == []  # no brackets, no match


@pytest.mark.asyncio
async def test_generate_answer_v2_inline_markers_end_to_end():
    payload = _payload()
    fake = FakeProvider([{"answer": "TECH101 carries 3 credits [E1]. Enrollment starts May 20, 2026 [E1].",
                          "refusal": False, "uncertainty": ""}])
    result = await generate_answer(payload, _runtime_config(), api_provider=fake)
    assert result["generation"]["schema_version"] == ANSWER_SCHEMA_VERSION
    assert result["generation"]["mode"] == "validated"
    assert all(claim["evidence_ids"] == ["d1:1:1"] for claim in result["claims"])
    assert result["citations"][0]["doc_uid"] == "d1"
    assert "[E1]" not in result["claims"][0]["text"]  # markers stripped from claim text


@pytest.mark.asyncio
async def test_generate_answer_model_refusal_is_not_mislabeled():
    payload = _payload()
    fake = FakeProvider([{"answer": "证据中未包含该信息，请补充学院与专业信息。", "refusal": True}])
    result = await generate_answer(payload, _runtime_config(), api_provider=fake)
    assert result["generation"]["mode"] == "refusal"
    assert result["generation"]["validated"] is True  # nothing asserted, nothing to check
    assert result["citations"] == [] and result["claims"] == []
