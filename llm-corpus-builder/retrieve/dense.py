"""Dense embedding index over corpus units, with a resumable npy cache.

The embedder is model-agnostic (anything sentence-transformers can load);
internal corpus text never leaves the process — models run locally.  The
cache layout mirrors the benchmarks' DocVQA pattern: one ``.npy`` memmap of
unit vectors plus a ``meta.json`` recording the model and unit count, so an
interrupted encode resumes instead of restarting.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Sequence

import numpy as np


_MODEL_CACHE: dict[tuple[str, str], Any] = {}


def _encoder(model_id: str, device: str = ""):
    from sentence_transformers import SentenceTransformer

    key = (model_id, device)
    if key not in _MODEL_CACHE:
        _MODEL_CACHE[key] = SentenceTransformer(model_id, device=device or None)
    return _MODEL_CACHE[key]


def embed_units(
    texts: Sequence[str],
    model_id: str,
    cache: Path,
    batch_size: int = 64,
    device: str = "",
) -> np.ndarray:
    """Encode texts into a unit matrix, reusing/continuing the cache file."""
    cache.parent.mkdir(parents=True, exist_ok=True)
    meta_path = cache.with_suffix(".meta.json")
    dim = 0
    done = 0
    if meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        # Compare by RESOLVED path: the same model passed once as an absolute
        # path and once relative ("models/embed/bge-m3") is the SAME model —
        # string inequality made every restart re-encode the whole corpus and
        # hold the GPU for minutes (caught live 2026-10-17).
        same_model = Path(str(meta.get("model", ""))).resolve() == Path(model_id).resolve()
        if same_model and cache.is_file():
            dim, done = meta["dim"], meta["count"]
            if done >= len(texts):
                return np.load(cache)[: len(texts)]
    if not done:
        # Peek the dimension with one encoded text, then allocate the memmap.
        print(f"[dense] encoding {len(texts)} units with {model_id} (cache miss; resumable)", file=sys.stderr)
        model = _encoder(model_id, device)
        dim = int(np.asarray(model.encode([texts[0]])).shape[1])
    else:
        model = _encoder(model_id, device)
    out = np.lib.format.open_memmap(cache, mode="r+" if done else "w+", shape=(len(texts), dim), dtype=np.float32)
    for start in range(done, len(texts), batch_size):
        batch = list(texts[start : start + batch_size])
        out[start : start + len(batch)] = np.asarray(model.encode(batch, batch_size=batch_size), dtype=np.float32)
        meta_path.write_text(json.dumps({"model": model_id, "dim": dim, "count": start + len(batch)}), encoding="utf-8")
    out.flush()
    # 一次性全量编码会让 CUDA 缓存分配器涨到数 GB 峰值且滞留进程终生
    # （16GB 卡上与另一套 runtime 并存即打满，2026-10-18 显存事故）。
    # 编码已落盘，归还预留显存，只留模型本身的常驻量。
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass
    return np.asarray(out)


def embed_query(question: str, model_id: str, device: str = "") -> np.ndarray:
    model = _encoder(model_id, device)
    return np.asarray(model.encode([question]), dtype=np.float32)[0]


def topk(matrix: np.ndarray, query: np.ndarray, k: int = 10) -> list[tuple[int, float]]:
    """Cosine top-k over an L2-normalized matrix."""
    norms = np.linalg.norm(matrix, axis=1)
    norms[norms == 0] = 1.0
    unit = matrix / norms[:, None]
    q = query / max(float(np.linalg.norm(query)), 1e-9)
    scores = unit @ q
    order = np.argpartition(-scores, min(k, len(scores) - 1))[:k]
    order = order[np.argsort(-scores[order], kind="stable")]
    return [(int(i), float(scores[i])) for i in order if scores[i] > 0]


def rrf_fuse(ranklists: list[list[tuple[int, float]]], k: int = 60) -> list[tuple[int, float]]:
    """Reciprocal-rank fusion of several (index, score) lists."""
    merged: dict[int, float] = {}
    for ranking in ranklists:
        for rank, (i, _) in enumerate(ranking, 1):
            merged[i] = merged.get(i, 0.0) + 1.0 / (k + rank)
    ordered = sorted(merged.items(), key=lambda kv: (-kv[1], kv[0]))
    return [(i, score) for i, score in ordered]
