"""P7.0: benchmark manifest freeze protocol."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench.manifest import BenchmarkManifest, load, schema_json


def _manifest(tmp_path: Path, **over) -> BenchmarkManifest:
    eval_file = tmp_path / "eval.jsonl"
    eval_file.write_text('{"id": "q1"}\n', encoding="utf-8")
    m = (BenchmarkManifest(track="A", system="A3_hybrid_rr", eval_file=eval_file)
         .with_raw_snapshot(tmp_path, 10, "deadbeef")
         .with_corpus_construction(chunker="recursive", chunk_size=512)
         .with_retrieval(retriever="hybrid_rr", bm25=True, rerank_model="models/rerank/bge-v2-m3")
         .with_evidence_pack(limit=8, query_ir=False)
         .with_generation(model="", prompt_version="")
         .with_eval(verified_only=True, today="2026-10-14"))
    for key, value in over.items():
        m.data[key] = value
    return m


def test_complete_manifest_saves_and_roundtrips(tmp_path: Path):
    m = _manifest(tmp_path)
    assert m.validate() == []
    path = m.save(tmp_path / "bm" / "a3.json")
    loaded = load(path)
    assert loaded["system"] == "A3_hybrid_rr"
    assert loaded["eval"]["verified_only"] is True
    assert len(loaded["eval"]["eval_sha256"]) == 64  # eval file pinned by hash


def test_missing_freeze_fields_refuse_the_run(tmp_path: Path):
    m = _manifest(tmp_path, retrieval={})  # retriever not pinned
    assert any("rerank_model" in e for e in m.validate())
    m2 = _manifest(tmp_path)
    m2.data["eval"].pop("verified_only")
    assert any("verified_only" in e for e in m2.validate())
    with pytest.raises(ValueError):
        m2.save(tmp_path / "bad.json")


def test_track_must_be_a_or_b(tmp_path: Path):
    with pytest.raises(ValueError):
        BenchmarkManifest(track="C", system="x", eval_file=tmp_path / "e.jsonl")


def test_schema_json_is_valid_json_with_required_keys():
    schema = json.loads(schema_json())
    assert set(schema["required"]) >= {"track", "retrieval", "eval"}
