# -*- coding: utf-8 -*-
"""Live smoke (2026-10-15): verify the latency optimizations against the real
LLM endpoint configured in the product .env — clean streaming answer text,
inline-marker schema v2, optimize cache hit.  No secrets are printed."""
import asyncio
import time

from product.config import load_config, llm_enabled
from product.providers import ApiProvider, EvidenceRef, _reset_query_cache
from product.answer import _BREAKER, generate_answer, _extract_inline_claims


async def main() -> None:
    config = load_config()
    assert llm_enabled(config), "LLM not configured in .env"
    provider = ApiProvider(config, breaker=_BREAKER)
    print(f"model={config.llm_model} max_tokens={config.llm_max_tokens} "
          f"stream_total={config.llm_stream_total_seconds}s idle={config.llm_stream_idle_seconds}s")

    # --- optimize: cold, then cached
    _reset_query_cache()
    t0 = time.perf_counter()
    q1 = await provider.optimize_queries("我毕业需要多少学分")
    cold_ms = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    q2 = await provider.optimize_queries("我毕业需要多少学分")
    warm_ms = (time.perf_counter() - t0) * 1000
    print(f"[optimize] cold={cold_ms:.0f}ms cached={warm_ms:.1f}ms queries={q1}")
    assert q1 == q2 and warm_ms < 50, "cache miss on repeat question"

    # --- generation: streaming visible text must be clean prose, not JSON
    refs = [
        EvidenceRef(index=1, evidence_id="d1:1:1", text="Minimum total credits for graduation: 128 credits, of which 7 are public degree courses.",
                    title="Undergraduate Curriculum 2026", source_url="https://example.test/curriculum", doc_id="d1"),
        EvidenceRef(index=2, evidence_id="d2:1:1", text="Application for degree conferral opens in week 8 of the final semester.",
                    title="Degree Guide", source_url="https://example.test/degree", doc_id="d2"),
    ]
    deltas: list[str] = []
    stamps: list[float] = []

    async def on_delta(text: str) -> None:
        stamps.append(time.perf_counter())
        deltas.append(text)

    t0 = time.perf_counter()
    raw = await provider.complete_structured("我毕业需要多少学分？什么时候能申请学位？", refs, conflict=False, on_delta=on_delta)
    total = time.perf_counter() - t0
    assert raw is not None, "generation failed"
    visible = "".join(deltas)
    ttft = (stamps[0] - t0) * 1000 if stamps else -1
    last_delta_at = (stamps[-1] - t0) * 1000 if stamps else -1
    print(f"[generate] total={total:.1f}s ttft={ttft:.0f}ms answer_done_at={last_delta_at:.0f}ms "
          f"tail_after_answer={(total - last_delta_at / 1000):.1f}s")
    print(f"[visible] starts={visible[:40]!r} ends={visible[-40:]!r}")
    assert not visible.lstrip().startswith("{"), "UI would see raw JSON"
    assert "\\n" not in visible and '\\"' not in visible, "undecoded escapes leaked"

    claims = _extract_inline_claims(str(raw.get("answer", "")), {f"E{r.index}": r.evidence_id for r in refs})
    print(f"[schema v2] refusal={raw.get('refusal')} follow_ups={raw.get('follow_ups')} extracted_claims={len(claims)}")
    for c in claims:
        print(f"  - {c['text'][:60]} -> {c['evidence_ids']}")
    assert claims, "no inline markers extracted"


if __name__ == "__main__":
    asyncio.run(main())
