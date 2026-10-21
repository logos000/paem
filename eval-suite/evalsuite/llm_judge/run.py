"""Judge orchestration: for each selected E2E row run the corpus-search agent
and the golden-answer scoring, attribute the failure, and aggregate."""

from __future__ import annotations

import json
import hashlib
import random
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from evalsuite import builder_root  # noqa: F401  (bridge)

from retrieve.lexical import BM25Index, Corpus  # noqa: E402  (builder, via bridge)

from evalsuite.llm_judge.agent import CorpusSearcher, run_agent  # noqa: E402
from evalsuite.llm_judge.gateway import Gateway  # noqa: E402
from evalsuite.llm_judge.scoring import DIMENSIONS, SCORING_VERSION, PROMPT_SHA256, score_answer  # noqa: E402


def load_corpus(corpus_dir: Path) -> CorpusSearcher:
    corpus = Corpus(corpus_dir)
    index = BM25Index.load(corpus.root / "_bm25" / "active.pkl")
    return CorpusSearcher(corpus, index)


def select_rows(e2e_rows: list[dict[str, Any]], questions: dict[str, dict[str, Any]], *,
                sample_validated: int = 25, seed: int = 7, strategy: str = "interesting") -> list[dict[str, Any]]:
    rows = [r for r in e2e_rows if r.get("id") in questions and r.get("decision")]
    if strategy == "all":
        return rows
    rng = random.Random(seed)
    picked: dict[str, dict[str, Any]] = {}
    def add(r: dict[str, Any]) -> None:
        picked[r["id"]] = r
    for r in rows:
        q = questions[r["id"]]
        mode = (r.get("generation") or {}).get("mode", "")
        abstain = r["decision"]["abstain"]
        if q.get("gold") and abstain:
            add(r)  # false abstain
        elif not q.get("gold") and not abstain:
            add(r)  # trap answered
        elif mode == "unverified":
            add(r)
    eligible = [r for r in rows if (r.get("generation") or {}).get("mode") == "validated"
                and questions[r["id"]].get("gold") and not r["decision"]["abstain"]]
    for r in rng.sample(eligible, min(sample_validated, len(eligible))):
        add(r)
    return [rows[i] for i in range(len(rows)) if rows[i]["id"] in picked]  # keep file order


def _attribute(question_row: dict[str, Any], e2e_row: dict[str, Any], score: dict[str, Any]) -> str:
    """Material existence is known BY CONSTRUCTION for generated questions:
    gold quotes are corpus text (lint-verified), so gold_insufficient marks
    the corpus/question-gap class and no search agent is needed (user ruling
    2026-10-15)."""
    answerable = bool(question_row.get("gold"))
    abstained = e2e_row["decision"]["abstain"]
    label = score.get("verdict_label", "")
    level = question_row.get("level")
    if label == "unparsed":
        return "judge_error"
    if question_row.get("reference_audit", {}).get("status") in ("invalid", "uncertain") or score.get("reference_status") in ("invalid", "uncertain"):
        return "reference_requires_review"
    if level == "C1":
        return "c1_boundary_held" if label == "good_refusal" else ("c1_supported_answer" if label == "good_answer" else "c1_requires_review")
    if level == "C5":
        safety = score.get("safety_verdict")
        if safety == "unsafe_assistance":
            return "unsafe_assistance"
        if safety == "overrefusal":
            return "safety_overrefusal"
        # Answer quality and safety are independent; never infer a violation
        # from an incomplete answer or a bad retrieval-layer refusal.
        return label if label in ("good_answer", "good_refusal", "incomplete_answer", "bad_refusal", "wrong_answer") else "c5_requires_review"
    if not answerable:
        return "trap_held" if abstained else "trap_violated"
    if question_row.get("gold_insufficient"):
        return "question_material_insufficient"  # the set's fault, not the system's
    if abstained:
        return "bad_refusal"  # material exists by construction
    if label == "good_answer":
        return "good_answer"
    if label in ("incomplete_answer",):
        return "incomplete_answer"
    return "answer_flaw"


def judge_row(e2e_row: dict[str, Any], question_row: dict[str, Any], gateway: Gateway) -> dict[str, Any]:
    golds = question_row.get("gold") or []
    score = score_answer(
        question_row["question"],
        str(question_row.get("golden_answer") or ""),
        golds,
        str(e2e_row.get("answer_text") or ""),
        e2e_row["decision"]["status"],
        e2e_row["decision"]["abstain"],
        gateway,
        level=str(question_row.get("level") or ""),
        evidence_pack=e2e_row.get("evidence_pack"), citation_map=e2e_row.get("citation_map"),
        requirements=question_row.get("requirements"), expected_behavior=question_row.get("expected_behavior", ""),
        answer_complete=e2e_row.get("answer_complete", False), reference_audit=question_row.get("reference_audit"),
        claims=e2e_row.get("claims") or [], citations=e2e_row.get("citations") or [],
        system_disposition=e2e_row.get("answer_disposition", ""),
    )
    return {
        "id": e2e_row["id"],
        "language": question_row.get("language"),
        "intent": question_row.get("intent"),
        "layer": question_row.get("layer"),
        "level": question_row.get("level"),
        "system_mode": (e2e_row.get("generation") or {}).get("mode"),
        "system_abstained": e2e_row["decision"]["abstain"],
        "gold_insufficient": bool(question_row.get("gold_insufficient")),
        "reference_status": score["reference_status"],
        "safety_verdict": score["safety_verdict"],
        "answer_disposition": score["answer_disposition"],
        "reference_provenance": question_row.get("provenance", "unreviewed"),
        "scoring_version": SCORING_VERSION,
        "score": score,
        "attribution": _attribute(question_row, e2e_row, score),
    }


def run_judge(e2e_json: Path, eval_jsonl: Path, out_path: Path, *,
              sample_validated: int = 25, workers: int = 20, strategy: str = "interesting",
              ids: list[str] | None = None) -> list[dict[str, Any]]:
    data = json.loads(e2e_json.read_text(encoding="utf-8"))
    questions = {json.loads(l)["id"]: json.loads(l) for l in eval_jsonl.open(encoding="utf-8")}
    e2e_rows = data["rows"]
    selected = select_rows(e2e_rows, questions, sample_validated=sample_validated, strategy=strategy) if not ids \
        else [r for r in e2e_rows if r.get("id") in set(ids)]
    done: set[str] = set()
    gateway = Gateway()
    build_ids = sorted({str(r.get("build_id")) for r in e2e_rows if r.get("build_id")})
    manifest = {"version": SCORING_VERSION, "prompt_sha256": PROMPT_SHA256, "model": gateway.model,
                "eval_sha256": hashlib.sha256(eval_jsonl.read_bytes()).hexdigest(),
                "e2e_sha256": hashlib.sha256(e2e_json.read_bytes()).hexdigest(),
                "selected_ids": [r["id"] for r in selected],
                "strategy": strategy if not ids else "explicit_ids",
                "sample_validated": sample_validated if not ids else None,
                "e2e_build_ids": build_ids,
                "question_count": len(questions)}
    manifest_path = out_path.with_suffix(".manifest.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists() and (not manifest_path.exists() or json.loads(manifest_path.read_text(encoding="utf-8")) != manifest):
        raise ValueError("judge inputs/version changed; use a new output path")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    if out_path.exists():
        done = {json.loads(l)["id"] for l in out_path.open(encoding="utf-8") if "error" not in json.loads(l)}
    todo = [r for r in selected if r["id"] not in done]
    print(f"[judge] {len(selected)} selected, {len(done)} already judged, {len(todo)} to run", file=sys.stderr)

    def _work(row: dict[str, Any]) -> dict[str, Any]:
        try:
            return judge_row(row, questions[row["id"]], gateway)
        except Exception as error:  # one row failing must not kill the run
            return {"id": row["id"], "error": f"{type(error).__name__}: {str(error)[:160]}"}

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a", encoding="utf-8") as handle, ThreadPoolExecutor(max_workers=workers) as pool:
        for i, result in enumerate(pool.map(_work, todo), 1):
            handle.write(json.dumps(result, ensure_ascii=False) + "\n")
            handle.flush()
            if i % 20 == 0:
                print(f"[judge] {i}/{len(todo)}", file=sys.stderr)
    # A retried failed row must not appear twice in aggregate statistics.
    return list({r["id"]: r for r in map(json.loads, out_path.read_text(encoding="utf-8").splitlines())}.values())


def aggregate(results: list[dict[str, Any]]) -> str:
    """Report each dimension with its own denominator, and safety separately."""
    good = [r for r in results if "score" in r and r.get("attribution") != "judge_error"]
    lines = [f"# 证据感知评审（{SCORING_VERSION}，n={len(good)}）", "",
             "不同版本评分不可直接比较。空值不计入均分；所有均分同时报告有效样本数。", "",
             f"评审错误行：{len(results) - len(good)}", "", "## 归因", ""]
    lines += [f"- {k}: {v}" for k, v in Counter(r["attribution"] for r in good).most_common()]
    cohorts = [("全部结果（含参考答案待复核）", good)]
    core = [r for r in good if r.get("level") in ("C2", "C3", "C4") and
            r.get("reference_status") == "valid" and not r.get("gold_insufficient")]
    cohorts.append(("C2–C4 参考答案有效核心集", core))
    for level in sorted({r.get("level") for r in good if r.get("level")}):
        cohorts.append((level, [r for r in good if r.get("level") == level]))
    for name, group in cohorts:
        lines += ["", f"## {name}（n={len(group)}）", "", "| 维度 | 均分 | 有效n |", "|---|---:|---:|"]
        for dim in DIMENSIONS:
            values = [r["score"]["scores"][dim] for r in group if r["score"]["scores"].get(dim) is not None]
            mean = f"{sum(values)/len(values):.3f}" if values else "—"
            lines.append(f"| {dim} | {mean} | {len(values)} |")
        complete = sum(r["score"]["verdict_label"] == "good_answer" for r in group)
        lines += ["", f"完整正确回答：{complete}/{len(group)}"]
    safety = [r for r in good if r.get("level") == "C5"]
    lines += ["", "## C5 独立安全判定", ""]
    lines += [f"- {k}: {v}" for k,v in Counter(r.get("safety_verdict", "uncertain") for r in safety).items()]
    lines += ["", "未答全与违规协助分开统计；C1不计入事实问答核心集。"]
    return "\n".join(lines) + "\n"
