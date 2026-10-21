"""Benchmark manifest: every cross-workflow experiment must be re-runnable
from one recorded file (P7.0 freeze protocol).

横向比较最容易出现的错误是"我们的系统用了更多信息或更多人工规则"。本模块
把 P7 文档 §3 的公平性条件落成机器可校验的 manifest：raw snapshot、语料构建
参数、检索/重排参数、EvidencePack 预算、生成配置、评测文件哈希与
verified-only 过滤方式、延迟与成本。任何系统都能用同一 manifest 重跑，不能
依赖未记录的环境变量或人工操作。

用法::

    from bench.manifest import BenchmarkManifest

    m = (BenchmarkManifest(track="A", system="A3_hybrid_rr", eval_file=..., corpus_build=...)
         .with_corpus_construction(chunker="recursive", chunk_size=512, chunk_stride=0, metadata_filter="published_year")
         .with_retrieval(bm25=True, dense_model="G:/model_download/builder/embed/bge-small-zh", dense_top_k=50,
                         rrf_k=60, rerank_model="G:/model_download/builder/rerank/bge-v2-m3", rerank_top_k=10)
         .with_evidence_pack(limit=8, query_ir=False, version_rules=False)
         .with_generation(model="", prompt_version="", temperature=0.0, max_tokens=0)  # Track A: none
         .with_eval(verified_only=True, today="2026-10-14"))
    m.save(path)

``load`` + ``validate``（对照 ``benchmark_manifest.schema.json`` 的手写校验）
用于跑前检查：缺一项关键公平性字段即拒绝执行。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

__all__ = ["BenchmarkManifest", "load", "REQUIRED_KEYS", "schema_json"]

SCHEMA_VERSION = "p7.0-v1"

# A manifest is only a freeze if every axis a baseline could quietly differ on
# is written down.  Missing any of these refuses the run (P7 doc §3).
REQUIRED_KEYS = (
    "track",               # A (corpus-only) | B (full workflow)
    "system",              # e.g. A0_raw_window / B2_hybrid_rr
    "raw_snapshot",        # crawl root + files hash summary
    "corpus_construction",
    "retrieval",
    "evidence_pack",
    "generation",
    "eval",
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class BenchmarkManifest:
    def __init__(self, *, track: str, system: str, eval_file: Path, corpus_build: Path | None = None, extra: dict[str, Any] | None = None):
        if track not in ("A", "B"):
            raise ValueError("track must be 'A' or 'B'")
        self.data: dict[str, Any] = {
            "schema": SCHEMA_VERSION,
            "track": track,
            "system": system,
            "corpus_build": str(corpus_build) if corpus_build else "",
            "corpus_build_id": "",
            "raw_snapshot": {},
            "corpus_construction": {},
            "retrieval": {},
            "evidence_pack": {},
            "generation": {},
            "eval": {"eval_file": str(eval_file), "eval_sha256": _sha256_file(eval_file) if eval_file.is_file() else ""},
            "timing": {},
            "extra": dict(extra or {}),
        }
        if corpus_build and (corpus_build / "manifest.json").is_file():
            try:
                self.data["corpus_build_id"] = json.loads((corpus_build / "manifest.json").read_text(encoding="utf-8")).get("build_id", "")
            except (OSError, ValueError):
                pass

    def with_raw_snapshot(self, crawl_root: Path, files: int, sha256: str) -> "BenchmarkManifest":
        self.data["raw_snapshot"] = {"crawl_root": str(crawl_root), "files": files, "sha256_of_hashes": sha256}
        return self

    def with_corpus_construction(self, **kwargs: Any) -> "BenchmarkManifest":
        self.data["corpus_construction"].update(kwargs)
        return self

    def with_retrieval(self, **kwargs: Any) -> "BenchmarkManifest":
        self.data["retrieval"].update(kwargs)
        return self

    def with_evidence_pack(self, **kwargs: Any) -> "BenchmarkManifest":
        self.data["evidence_pack"].update(kwargs)
        return self

    def with_generation(self, **kwargs: Any) -> "BenchmarkManifest":
        self.data["generation"].update(kwargs)
        return self

    def with_eval(self, **kwargs: Any) -> "BenchmarkManifest":
        self.data["eval"].update(kwargs)
        return self

    def with_timing(self, **kwargs: Any) -> "BenchmarkManifest":
        self.data["timing"].update(kwargs)
        return self

    def validate(self) -> list[str]:
        errors = [f"missing required key: {key}" for key in REQUIRED_KEYS if not self.data.get(key)]
        retrieval = self.data["retrieval"]
        if "retriever" not in retrieval:
            errors.append("retrieval.retriever must be recorded explicitly")
        # Track A 只能改变语料构建。与冻结栈（BM25→bge-v2-m3）可比的系统必须
        # 钉死 rerank_model；外部方法族（naive/dense）本身不含重排器，但同样
        # 必须显式记录 rerank_model=""（"没有"也是要写下来的配置）。
        if "rerank_model" not in retrieval:
            errors.append("Track A must record rerank_model explicitly (empty = this family has no reranker)")
        if "verified_only" not in self.data["eval"]:
            errors.append("eval.verified_only must be stated explicitly")
        return errors

    def save(self, path: Path) -> Path:
        errors = self.validate()
        if errors:
            raise ValueError("; ".join(errors))
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        return path


def load(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def schema_json() -> str:
    """The JSON Schema for bench manifests (also written next to results)."""
    return json.dumps({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "llm-corpus-builder benchmark manifest",
        "type": "object",
        "required": list(REQUIRED_KEYS),
        "properties": {
            "schema": {"const": SCHEMA_VERSION},
            "track": {"enum": ["A", "B"]},
            "system": {"type": "string"},
            "corpus_build": {"type": "string"},
            "corpus_build_id": {"type": "string"},
            "raw_snapshot": {"type": "object", "required": ["crawl_root", "sha256_of_hashes"]},
            "corpus_construction": {"type": "object"},
            "retrieval": {"type": "object"},
            "evidence_pack": {"type": "object"},
            "generation": {"type": "object"},
            "eval": {"type": "object", "required": ["eval_file", "eval_sha256", "verified_only"]},
            "timing": {"type": "object"},
            "extra": {"type": "object"},
        },
    }, ensure_ascii=False, indent=1)
