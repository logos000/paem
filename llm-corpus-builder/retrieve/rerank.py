"""Local cross-encoder reranking for the evidence pipeline.

Rerankers only reorder BM25/IR candidates by relevance.  Hard applicability
rules (term / audience / version) stay in the IR layer and outrank the
reranker score, so a mismatched document can never cross back to the top.
All models run locally on this machine — internal corpus content must never
be sent to an external API.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, Sequence

MODEL_ALIASES: dict[str, str] = {
    "bge-base": "BAAI/bge-reranker-base",
    "bge-v2-m3": "BAAI/bge-reranker-v2-m3",
    "qwen3-0.6b": "Qwen/Qwen3-Reranker-0.6B",
}

_LOCAL_ROOT = Path(__file__).resolve().parents[1] / "models" / "rerank"


def _resolve(alias: str) -> str:
    """Prefer a locally downloaded copy under models/rerank/<alias>."""
    local = _LOCAL_ROOT / alias
    if (local / "config.json").is_file():
        return str(local)
    return MODEL_ALIASES.get(alias, alias)

MAX_LENGTH = 512
BATCH_SIZE = 16


class Reranker(Protocol):
    name: str

    def score(self, pairs: Sequence[tuple[str, str]]) -> list[float]:
        """Score (query, text) pairs; higher means more relevant."""
        ...


class CrossEncoderReranker:
    """Standard cross-encoder scoring via sentence-transformers."""

    def __init__(self, alias: str, model_id: str, device: str = "") -> None:
        from sentence_transformers import CrossEncoder

        self.name = alias
        self._model = CrossEncoder(model_id, max_length=MAX_LENGTH, device=device or None)

    def score(self, pairs: Sequence[tuple[str, str]]) -> list[float]:
        return [float(v) for v in self._model.predict(list(pairs), batch_size=BATCH_SIZE)]


class Qwen3Reranker:
    """Qwen3-Reranker: P(yes) / (P(yes) + P(no)) from a causal LM."""

    _PREFIX = (
        "<|im_start|>system\nJudge whether the Document meets the requirements "
        'based on the Query. Note that the answer must be\n "yes" or "no".'
        "<|im_end|>\n<|im_start|>user\n"
    )
    _SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"

    def __init__(self, alias: str, model_id: str, device: str = "") -> None:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self.name = alias
        self._torch = torch
        if not device:
            device = "cuda" if torch.cuda.is_available() else "cpu"
        self._device = device
        dtype = torch.bfloat16 if device.startswith("cuda") else torch.float32
        self._tokenizer = AutoTokenizer.from_pretrained(model_id, padding_side="left")
        self._model = AutoModelForCausalLM.from_pretrained(model_id, torch_dtype=dtype).to(device)
        self._model.eval()
        self._yes = self._tokenizer("yes", add_special_tokens=False).input_ids[0]
        self._no = self._tokenizer("no", add_special_tokens=False).input_ids[0]

    def score(self, pairs: Sequence[tuple[str, str]]) -> list[float]:
        torch = self._torch
        scores: list[float] = []
        with torch.no_grad():
            for start in range(0, len(pairs), BATCH_SIZE):
                batch = pairs[start : start + BATCH_SIZE]
                prompts = [
                    f"{self._PREFIX}Query: {query}\nDocument: {text}{self._SUFFIX}"
                    for query, text in batch
                ]
                encoded = self._tokenizer(
                    prompts, padding=True, truncation=True, max_length=MAX_LENGTH * 2, return_tensors="pt"
                ).to(self._device)
                logits = self._model(**encoded).logits[:, -1, :]
                choice = logits[:, [self._no, self._yes]].float()
                probs = torch.softmax(choice, dim=-1)[:, 1]
                scores.extend(float(v) for v in probs)
        return scores


_CACHE: dict[str, Reranker] = {}


def get_reranker(alias: str, device: str = "") -> Reranker:
    """Load (and cache) a reranker by alias; one model resident at a time."""
    model_id = _resolve(alias)
    if alias in _CACHE:
        return _CACHE[alias]
    release()
    if "qwen3" in alias.lower():
        reranker: Reranker = Qwen3Reranker(alias, model_id, device)
    else:
        reranker = CrossEncoderReranker(alias, model_id, device)
    _CACHE[alias] = reranker
    return reranker


def release() -> None:
    """Free the resident reranker and its GPU memory."""
    _CACHE.clear()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass


def rerank_candidates(
    question: str,
    candidates: list[tuple[int, float, dict[str, Any]]],
    reranker: Reranker,
    texts: Sequence[str],
    k: int = 10,
) -> tuple[list[tuple[int, float]], float]:
    """Re-sort IR candidates by relevance under hard rules.

    Applicability (mismatches) and superseded flags from ``ir_rerank`` stay
    the primary sort keys; the reranker only orders within those tiers.
    Returns ``(ordered (unit_index, bm25_score), seconds)``.
    """
    import time as _time

    if not candidates:
        return [], 0.0
    started = _time.perf_counter()
    pairs = [(question, texts[i]) for i, _, _ in candidates]
    scores = reranker.score(pairs)
    order = sorted(
        range(len(candidates)),
        key=lambda pos: (
            bool(candidates[pos][2].get("mismatches")),
            bool(candidates[pos][2].get("superseded")),
            -scores[pos],
            -candidates[pos][1],
        ),
    )
    ranked = [(candidates[pos][0], candidates[pos][1]) for pos in order[:k]]
    return ranked, _time.perf_counter() - started
