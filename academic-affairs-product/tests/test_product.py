"""Product tests against a STUB corpus runtime (same HTTP contract).

The stub speaks the answer-context contract (llm-corpus-builder
runtime/answer_context.schema.json); the product must consume it without any
retrieval logic of its own.  Real-engine equivalence (in-process == HTTP ==
product) lives in the builder repo's tests.
"""

from __future__ import annotations

from typing import Any

import pytest

fastapi_test = pytest.importorskip("fastapi.testclient")
import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402


def _evidence(item_id: str, text: str, *, title: str = "Course Guide", url: str = "https://example.test/guide") -> dict[str, Any]:
    return {
        "evidence_id": item_id,
        "doc_id": "d1",
        "title": title,
        "text": text,
        "blocking": False,
        "provenance": {"doc_uid": "d1", "block_id": "d1#b1", "source_url": url},
    }


def _payload(status: str = "sufficient", abstain: bool = False) -> dict[str, Any]:
    items = [_evidence("d1:1:1", "Enrollment starts 8:00 on May 20."), _evidence("d1:2:2", "Late enrollment needs dean approval.", title="Handbook")]
    return {
        "version": 2,
        "question": "When does enrollment start?",
        "today": None,
        "config": "active_llm_validated_ir_rr",
        "pack_hash": "ab" * 32,
        "retrieval_trace": {"strategy": "single_query", "queries": ["When does enrollment start?"], "rounds": []},
        "query_ir": {"raw": "When does enrollment start?", "language": "en", "terms": [], "academic_years": [], "cohorts": [], "audience": "", "course_codes": []},
        "user_context": {},
        "decision": {"status": status, "abstain": abstain, "reason": "requested term has no applicable evidence" if abstain else ""},
        "evidence_pack": {
            "query": "When does enrollment start?",
            "query_ir": {},
            "evidence": items,
            "coverage": 0.5,
            "conflicts": [],
            "usable_evidence_ids": [items[0]["evidence_id"]],
            "evidence_status": status,
            "abstain": abstain,
            "abstain_reason": "requested term has no applicable evidence" if abstain else "",
        },
        "provenance": {"build_id": "test-build", "corpus_root": "x", "config": "active_ir", "rerank_model": "", "dense_model": "", "n_units": 3, "n_documents": 2, "n_known_conflicts": 0, "gated_arm": True},
        "latency_ms": 12.5,
    }


def _stub_runtime(payload: dict[str, Any] | list[dict[str, Any]], *, status_code: int = 200) -> FastAPI:
    from fastapi import HTTPException

    app = FastAPI()
    sequence = list(payload) if isinstance(payload, list) else None

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "build_id": "test-build", "config": "active_ir", "n_units": 3, "version": 2}

    @app.post("/v1/answer_context")
    def answer_context() -> Any:
        if status_code != 200:
            raise HTTPException(status_code=status_code, detail="bad request")
        return sequence.pop(0) if sequence is not None else payload

    return app


def _product_client(payload: dict[str, Any] | list[dict[str, Any]], monkeypatch, tmp_path, *, status_code: int = 200, llm: bool = False):
    from contextlib import contextmanager

    from product.app import app
    from product import app as app_module

    # Tests are deterministic template-mode by default: never hit a real LLM
    # even when the developer's product .env carries live keys.  llm=True
    # points the config at a fake endpoint for scripted-provider tests.
    for llm_key, value in (("LLM_BASE_URL", "http://fake.test" if llm else ""),
                           ("LLM_API_KEY", "k" if llm else ""),
                           ("LLM_MODEL", "test-model" if llm else "")):
        monkeypatch.setenv(llm_key, value)
    stub = _stub_runtime(payload, status_code=status_code)
    monkeypatch.setattr(app_module, "CorpusRuntimeClient", lambda base_url, timeout=90.0: _StubClient(stub))
    monkeypatch.setenv("ASK_LOG", str(tmp_path / "ask.jsonl"))

    @contextmanager
    def _client():
        with fastapi_test.TestClient(app) as client:
            yield client

    return _client()


def test_ask_returns_answer_and_citations(monkeypatch, tmp_path):
    with _product_client(_payload(), monkeypatch, tmp_path) as client:
        response = client.post("/v1/ask", json={"question": "When does enrollment start?"})
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["build_id"] == "test-build"
        assert body["decision"]["status"] == "sufficient"
        assert body["provider"] == "evidence-template"  # no LLM env in tests
        assert body["citations"][0]["source_url"] == "https://example.test/guide"
        assert "May 20" in body["answer"]
        assert body["evidence_pack"]["usable_evidence_ids"] == ["d1:1:1"]
        assert client.get("/health").json()["runtime"]["build_id"] == "test-build"


def test_ask_refusal_is_structured(monkeypatch, tmp_path):
    with _product_client(_payload(status="wrong_term", abstain=True), monkeypatch, tmp_path) as client:
        body = client.post("/v1/ask", json={"question": "What happens in 2031 spring?", "user_context": {"audience": "undergraduate"}}).json()
        assert body["decision"]["abstain"] is True
        assert body["citations"] == []
        assert "暂不作出确定回答" in body["answer"]


def test_ask_writes_log_and_propagates_runtime_error(monkeypatch, tmp_path):
    with _product_client(_payload(), monkeypatch, tmp_path, status_code=400) as client:
        response = client.post("/v1/ask", json={"question": "?"})
        assert response.status_code == 502  # runtime error surfaces as bad gateway
    log_lines = (tmp_path / "ask.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(log_lines) == 1 and "error" in log_lines[0]


# ------------------------------------------------------------------ P0: contract & config

def test_contract_mismatch_returns_dependency_contract_error(monkeypatch, tmp_path):
    broken = {**_payload(), "version": 3}  # future contract version
    with _product_client(broken, monkeypatch, tmp_path) as client:
        response = client.post("/v1/ask", json={"question": "When does enrollment start?"})
    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["error"] == "dependency_contract_error"
    assert detail["problems"]


def test_missing_decision_field_is_contract_error(monkeypatch, tmp_path):
    broken = _payload()
    broken["decision"] = {"abstain": False}  # no status
    with _product_client(broken, monkeypatch, tmp_path) as client:
        response = client.post("/v1/ask", json={"question": "?"})
    assert response.status_code == 502
    assert response.json()["detail"]["error"] == "dependency_contract_error"


def test_disallowed_arm_fails_startup_with_config_kind(monkeypatch, tmp_path):
    from product.app import app

    monkeypatch.setenv("PRODUCT_ARM", "oracle_doc")  # eval-only arm: not allowed
    monkeypatch.setenv("ASK_LOG", str(tmp_path / "ask.jsonl"))
    import pytest

    with pytest.raises(RuntimeError, match=r"\[config\]"):
        with fastapi_test.TestClient(app):
            pass


def test_partial_llm_env_is_config_error(monkeypatch):
    from product.config import ConfigError, load_config

    monkeypatch.setenv("LLM_BASE_URL", "https://api.example.com")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    with pytest.raises(ConfigError) as error:
        load_config()
    assert error.value.kind == "config"


def test_health_live_ready_and_version(monkeypatch, tmp_path):
    monkeypatch.setenv("PRODUCT_ARM", "sp_lexical")  # deterministic lexical arm for tests (sp defaults to hybrid)
    with _product_client(_payload(), monkeypatch, tmp_path) as client:
        live = client.get("/health/live").json()
        assert live["ok"] is True and live["service"] == "product"
        ready = client.get("/health/ready").json()
        assert ready["ok"] is True
        assert ready["arm"] == "active_ir"
        assert ready["runtime"]["build_id"] == "test-build"
        version = client.get("/version").json()
        assert version["contract_version"] == 2
        assert version["runtime"]["build_id"] == "test-build"


def test_ask_reports_stage_timings(monkeypatch, tmp_path):
    with _product_client(_payload(), monkeypatch, tmp_path) as client:
        body = client.post("/v1/ask", json={"question": "When does enrollment start?"}).json()
    timing = body["timing"]
    assert set(timing) == {"retrieval_ms", "builder_ms", "llm_ms", "total_ms"}
    assert timing["retrieval_ms"] == 12.5  # engine latency from the payload
    assert timing["total_ms"] >= timing["builder_ms"]


# ------------------------------------------------------------------ P2: capacity, metrics, redaction

def test_rate_limit_returns_429(monkeypatch, tmp_path):
    from product import app as app_module
    from product.ratelimit import RateLimiter

    monkeypatch.setattr(app_module, "_ASK_LIMITER", RateLimiter(rate_per_minute=0.1, burst=2))
    with _product_client(_payload(), monkeypatch, tmp_path) as client:
        codes = [client.post("/v1/ask", json={"question": "?"}).status_code for _ in range(5)]
    assert codes[0] == 200 and codes[1] == 200
    assert 429 in codes[2:]


def test_metrics_endpoint_counts_requests(monkeypatch, tmp_path):
    with _product_client(_payload(), monkeypatch, tmp_path) as client:
        client.post("/v1/ask", json={"question": "When does enrollment start?"})
        metrics = client.get("/metrics").json()
    assert metrics["counters"]["ask_total"] >= 1
    assert metrics["counters"]["decision_sufficient"] >= 1
    assert metrics["current_build"] == "test-build"
    assert metrics["latency_ms"]["total"]["p50"] > 0


def test_log_redaction(monkeypatch, tmp_path):
    import json as jsonlib

    monkeypatch.setenv("ASK_LOG", str(tmp_path / "ask.jsonl"))
    monkeypatch.delenv("ASK_LOG_FULL", raising=False)
    from product.log import log_ask, redact

    assert "[email]" in redact("contact zhang.san@example.com please")
    assert "[phone]" in redact("call 13800000000 now")
    log_ask({"request_id": "r", "question": "email li.si@example.com about 3230100001 scholarship policy question"})
    line = jsonlib.loads((tmp_path / "ask.jsonl").read_text(encoding="utf-8").strip())
    assert "li.si@example.com" not in line["question"]
    assert "[email]" in line["question"] and "[student-id]" in line["question"]
    assert len(line["question"]) <= 120


# ------------------------------------------------------------------ P5: sessions, feedback, SSE

def test_session_records_ask_turns(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_STORE", str(tmp_path / "sessions.jsonl"))
    with _product_client(_payload(), monkeypatch, tmp_path) as client:
        session = client.post("/v1/session").json()
        assert session["session_id"].startswith("s-")
        body = client.post("/v1/ask", json={"question": "When does enrollment start?", "session_id": session["session_id"]}).json()
        turns = client.get(f"/v1/session/{session['session_id']}").json()["turns"]
    assert len(turns) == 1
    assert turns[0]["request_id"] == body["request_id"]
    assert turns[0]["decision"]["status"] == "sufficient"


def test_feedback_persisted(monkeypatch, tmp_path):
    monkeypatch.setenv("FEEDBACK_STORE", str(tmp_path / "feedback.jsonl"))
    with _product_client(_payload(), monkeypatch, tmp_path) as client:
        response = client.post("/v1/feedback", json={"request_id": "req-1", "helpful": True, "comment": "great"})
        assert response.json()["ok"] is True
    import json as jsonlib

    record = jsonlib.loads((tmp_path / "feedback.jsonl").read_text(encoding="utf-8").strip())
    assert record["request_id"] == "req-1" and record["helpful"] is True


def test_ask_stream_emits_stages_then_result(monkeypatch, tmp_path):
    import json as jsonlib

    with _product_client(_payload(), monkeypatch, tmp_path) as client:
        events = []
        with client.stream("POST", "/v1/ask/stream", json={"question": "When does enrollment start?"}) as response:
            assert response.status_code == 200
            for line in response.iter_lines():
                if line.startswith("data: "):
                    events.append(jsonlib.loads(line[len("data: "):]))
    stages = [event["name"] for event in events if event["type"] == "stage"]
    results = [event for event in events if event["type"] == "result"]
    # no LLM in tests -> optimizing_query fires but instantly transitions
    assert stages[0] == "optimizing_query"
    assert "retrieving" in stages and "generating" in stages and "validating" in stages and stages[-1] == "done"
    retrieved = [event for event in events if event["type"] == "retrieved"]
    assert retrieved and retrieved[0]["n_evidence"] >= 1 and retrieved[0]["documents"][0]["title"] == "Course Guide"
    deltas = [event for event in events if event["type"] == "answer_delta"]
    assert deltas and "".join(d["text"] for d in deltas)  # template emits its answer as one delta
    assert len(results) == 1 and results[0]["payload"]["ok"] is True
    assert results[0]["payload"]["query_ir"]["language"] == "en"


# ------------------------------------------------------------------ Stage 5 V3: material import

def test_material_upload_lands_in_inbox_and_lists_queued(monkeypatch, tmp_path):
    import json as jsonlib

    monkeypatch.delenv("PRODUCT_ADMIN_PASSWORD", raising=False)  # gate covered by its own test
    monkeypatch.setenv("INBOX_DIR", str(tmp_path / "inbox"))
    with _product_client(_payload(), monkeypatch, tmp_path) as client:
        upload = client.post(
            "/v1/materials",
            files={"file": ("notice.pdf", b"%PDF-test-notice", "application/pdf")},
            data={"kind": "pdf", "site": "sharepoint", "title": "Test Notice"},
        )
        assert upload.status_code == 200
        body = upload.json()
        assert body["status"] == "queued" and body["job_id"]
        listing = client.get("/v1/materials").json()
    assert listing["pending"] == 1
    item = listing["items"][0]
    assert item["stage"] == "queued" and item["file"] == "notice.pdf"
    inbox = tmp_path / "inbox"
    saved = list((inbox / "uploads").glob("*.pdf"))
    assert saved and saved[0].read_bytes() == b"%PDF-test-notice"
    job = jsonlib.loads((inbox / "jobs" / f"{body['job_id']}.json").read_text(encoding="utf-8"))
    assert job["kind"] == "pdf" and job["file"].endswith(".pdf") and job["filename"] == "notice.pdf"


def test_material_validation_rejects(monkeypatch, tmp_path):
    monkeypatch.delenv("PRODUCT_ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("INBOX_DIR", str(tmp_path / "inbox"))
    with _product_client(_payload(), monkeypatch, tmp_path) as client:
        bad_kind = client.post("/v1/materials", files={"file": ("x.pdf", b"x", "application/pdf")}, data={"kind": "video"})
        assert bad_kind.status_code == 400
        bad_ext = client.post("/v1/materials", files={"file": ("x.mp4", b"x", "video/mp4")}, data={"kind": "pdf"})
        assert bad_ext.status_code == 400
        page_no_url = client.post("/v1/materials", files={"file": ("n.html", b"<html>", "text/html")}, data={"kind": "page", "site": "zjui", "title": "t"})
        assert page_no_url.status_code == 400


def test_material_status_merges_watcher_events(monkeypatch, tmp_path):
    import json as jsonlib

    monkeypatch.delenv("PRODUCT_ADMIN_PASSWORD", raising=False)
    inbox = tmp_path / "inbox"
    (inbox / "jobs").mkdir(parents=True)
    (inbox / "uploads").mkdir()
    (inbox / "jobs" / "job-9.json").write_text(jsonlib.dumps({"id": "job-9", "file": "a.pdf", "filename": "a.pdf", "kind": "pdf", "title": "", "site": "sharepoint"}), encoding="utf-8")
    with (inbox / "status.jsonl").open("w", encoding="utf-8") as handle:
        handle.write(jsonlib.dumps({"ts": "t1", "job_id": "job-9", "stage": "published", "build_id": "build-xyz"}) + "\n")
    monkeypatch.setenv("INBOX_DIR", str(inbox))
    with _product_client(_payload(), monkeypatch, tmp_path) as client:
        listing = client.get("/v1/materials").json()
    item = listing["items"][0]
    assert item["stage"] == "published" and item["build_id"] == "build-xyz" and listing["pending"] == 0


class _StubClient:
    """CorpusRuntimeClient shape over an in-process stub app (ASGI transport),
    mirroring its non-200 → CorpusRuntimeError semantics."""

    def __init__(self, stub: FastAPI):
        self._client = httpx.AsyncClient(transport=httpx.ASGITransport(app=stub), base_url="http://stub")

    async def health(self):
        response = await self._client.get("/health")
        if response.status_code != 200:
            from product.corpus_client import CorpusRuntimeError
            raise CorpusRuntimeError(f"/health -> {response.status_code}", response.status_code)
        return response.json()

    async def answer_context(self, **kwargs: Any) -> dict[str, Any]:
        response = await self._client.post("/v1/answer_context", json=kwargs)
        if response.status_code != 200:
            from product.corpus_client import CorpusRuntimeError
            raise CorpusRuntimeError(f"-> {response.status_code}: {response.text[:200]}", response.status_code)
        return response.json()

    async def aclose(self):
        await self._client.aclose()



# ------------------------- unified status / diagnostics / follow-up pack rebuild (2026-10-16)

def _unlimited_asks(monkeypatch):
    """Fresh generous limiter: the module-level one carries token state from
    earlier tests in the same file (burst 10 < the file's ask count)."""
    from product import app as app_module
    from product.ratelimit import RateLimiter
    monkeypatch.setattr(app_module, "_ASK_LIMITER", RateLimiter(rate_per_minute=1e9, burst=10_000))


def test_ask_response_carries_status_diagnostics_pack_hash(monkeypatch, tmp_path):
    _unlimited_asks(monkeypatch)
    with _product_client(_payload(), monkeypatch, tmp_path) as client:
        body = client.post("/v1/ask", json={"question": "When does enrollment start?"}).json()
    assert body["status"] == "answered"          # template mode, evidence verbatim
    assert body["pack_hash"] == "ab" * 32
    assert body["diagnostics"]["decision_status"] == "sufficient"
    assert body["diagnostics"]["pack_hash"] == body["pack_hash"]
    assert body["diagnostics"]["build_id"] == "test-build"
    assert body["diagnostics"]["generation_mode"]
    assert body["diagnostics"]["validation_problems"] == []


def test_ask_refusal_status_is_refused_evidence(monkeypatch, tmp_path):
    _unlimited_asks(monkeypatch)
    with _product_client(_payload(status="wrong_term", abstain=True), monkeypatch, tmp_path) as client:
        body = client.post("/v1/ask", json={"question": "?"}).json()
    assert body["status"] == "refused_evidence"
    assert body["diagnostics"]["decision_status"] == "wrong_term"


class _ScriptedApiProvider:
    """ApiProvider stand-in: one shared script popped per complete_structured."""

    name = "fake-api"
    model = None
    script: list[dict[str, Any]] = []

    def __init__(self, config, breaker=None, transport=None):
        pass

    async def optimize_queries(self, question: str):
        return None  # keep the single-query path

    async def complete_structured(self, question, refs, **kwargs):
        return self.script.pop(0) if self.script else None


def test_follow_up_rebuilds_pack_and_rebinds_pack_hash(monkeypatch, tmp_path):
    _unlimited_asks(monkeypatch)
    monkeypatch.setenv("FEATURE_FOLLOW_UP_RETRIEVAL", "1")  # off by default since the 2026-10-16 convergence
    import product.providers as providers
    from product import app as app_module

    payload_a = _payload()
    payload_b = _payload()
    payload_b["pack_hash"] = "cd" * 32
    payload_b["evidence_pack"] = {
        **payload_b["evidence_pack"],
        "evidence": [_evidence("d9:1:1", "Enrollment opens 9:00 on May 21.")],
        "usable_evidence_ids": ["d9:1:1"],
    }
    _ScriptedApiProvider.script = [
        {"answer": "Enrollment starts 8:00 on May 20. [E1]",
         "claims": [{"text": "Enrollment starts 8:00 on May 20.", "evidence_ids": ["E1"]}],
         "refusal": False, "follow_ups": ["late enrollment deadline"]},
        {"answer": "Enrollment opens 9:00 on May 21. [E1]",
         "claims": [{"text": "Enrollment opens 9:00 on May 21.", "evidence_ids": ["E1"]}],
         "refusal": False, "follow_ups": []},
    ]
    monkeypatch.setattr(providers, "ApiProvider", _ScriptedApiProvider)
    import product.answer as answer_module
    monkeypatch.setattr(answer_module, "ApiProvider", _ScriptedApiProvider)  # generate_answer binds it at import time
    monkeypatch.setattr(app_module, "_BREAKER_STATE", None, raising=False)
    with _product_client([payload_a, payload_b], monkeypatch, tmp_path, llm=True) as client:
        body = client.post("/v1/ask", json={"question": "When does enrollment start?"}).json()
    # the answer must be bound to the FINAL pack: hash + claims rebound to d9
    assert body["pack_hash"] == "cd" * 32
    assert body["diagnostics"]["pack_hash"] == "cd" * 32
    assert body["claims"][0]["evidence_ids"] == ["d9:1:1"]
    assert body["status"] == "answered"


def test_follow_up_build_change_retains_original_answer(monkeypatch, tmp_path):
    _unlimited_asks(monkeypatch)
    monkeypatch.setenv("FEATURE_FOLLOW_UP_RETRIEVAL", "1")
    import product.providers as providers
    from product import app as app_module

    payload_a = _payload()
    payload_b = _payload()
    payload_b["pack_hash"] = "ef" * 32
    payload_b["provenance"] = {**payload_b["provenance"], "build_id": "other-build"}
    _ScriptedApiProvider.script = [
        {"answer": "Enrollment starts 8:00 on May 20. [E1]",
         "claims": [{"text": "Enrollment starts 8:00 on May 20.", "evidence_ids": ["E1"]}],
         "refusal": False, "follow_ups": ["late enrollment deadline"]},
    ]
    monkeypatch.setattr(providers, "ApiProvider", _ScriptedApiProvider)
    import product.answer as answer_module
    monkeypatch.setattr(answer_module, "ApiProvider", _ScriptedApiProvider)  # generate_answer binds it at import time
    monkeypatch.setattr(app_module, "_BREAKER_STATE", None, raising=False)
    with _product_client([payload_a, payload_b], monkeypatch, tmp_path, llm=True) as client:
        body = client.post("/v1/ask", json={"question": "When does enrollment start?"}).json()
    # a mid-request build swap must never lose or rebind the first answer
    assert body["build_id"] == "test-build"
    assert body["pack_hash"] == "ab" * 32
    assert body["claims"][0]["evidence_ids"] == ["d1:1:1"]
    assert body["status"] == "answered"


def test_evidence_context_line_renders_table_provenance():
    from product.answer import _evidence_context
    from product.providers import EvidenceRef, build_prompt

    class Item:
        heading = "Junior"
        table_context = {"table_title": "Junior", "header": ["No.", "Time", "Wednesday", "", ""]}
        text = "No.: 19 | Time: 18:00-18:30 | ME 360 (LAB) M1 (20)"

    line = _evidence_context(Item())
    assert "位置:Junior" in line and "列头:No. | Time | Wednesday" in line

    class Plain:
        heading = ""
        table_context = {}

    assert _evidence_context(Plain()) == ""

    prompt = build_prompt("question", [EvidenceRef(index=1, evidence_id="e1", text="row", context=line)], conflict=False)
    assert "〔位置:Junior" in prompt and "row" in prompt


# ---- admin gate for public tunnels (2026-10-17) ----

def test_admin_gate_blocks_without_password(monkeypatch, tmp_path):
    import base64
    monkeypatch.setenv("PRODUCT_ADMIN_PASSWORD", "adm-secret")
    monkeypatch.setenv("ASK_LOG", str(tmp_path / "ask.jsonl"))
    from product.app import app
    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        assert client.get("/admin/builds").status_code == 401
        # materials upload/list is switch-equivalent power (upload → auto-build
        # → auto-switch) and shares the gate
        assert client.get("/v1/materials").status_code == 401
        assert client.post("/v1/materials").status_code == 401
        token = base64.b64encode(b"admin:adm-secret").decode()
        # with the right credentials the proxy proceeds (may 503 if runtime down — not 401)
        assert client.get("/admin/builds", headers={"Authorization": f"Basic {token}"}).status_code != 401
        assert client.get("/v1/materials", headers={"Authorization": f"Basic {token}"}).status_code != 401
        # non-admin routes stay open
        assert client.get("/health/live").status_code == 200


def test_admin_gate_off_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("PRODUCT_ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("ASK_LOG", str(tmp_path / "ask.jsonl"))
    from product.app import app
    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200


def test_admin_feedback_joins_turn_by_request_id(monkeypatch, tmp_path):
    # reviewer round 1, item 5: a 👎 must show the full ask, not just a counter
    monkeypatch.delenv("PRODUCT_ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("SESSION_STORE", str(tmp_path / "sessions.jsonl"))
    monkeypatch.setenv("FEEDBACK_STORE", str(tmp_path / "feedback.jsonl"))
    from product.sessions import append_feedback, append_turn
    append_turn("s-1", {
        "request_id": "req-1", "question": "退课截止日期?", "decision": {"status": "sufficient", "abstain": False, "reason": ""},
        "answer": "11月6日", "provider": "p", "build_id": "b", "config": "c",
        "status": "answered", "citations": [{"id": "e1", "title": "Guide", "source_url": "u", "doc_uid": "d1", "block_id": ""}],
    })
    append_feedback({"request_id": "req-1", "helpful": False, "comment": "没答到点"})
    from product.app import app
    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        body = client.get("/admin/feedback").json()
    assert body["n"] == 1
    row = body["rows"][0]
    assert row["helpful"] is False and row["question"].startswith("退课")
    assert row["status"] == "answered" and row["citations"][0]["doc_uid"] == "d1"
    assert row["answer"] == "11月6日"
