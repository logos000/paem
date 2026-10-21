"""The corpus search agent: the judge model drives its own BM25 query loop
to rule whether answer-bearing material exists in the corpus.
"""

from __future__ import annotations

import re
from typing import Any

from evalsuite.llm_judge.gateway import Gateway, extract_json

MAX_ROUNDS = 4
TOP_K = 8

SYSTEM_PROMPT = """你是语料核查代理。你的任务是：独立判断语料库中【是否存在能够回答指定问题的材料】。
你不知道别的系统检索到了什么——只依据你自己的搜索下结论。

每一轮你可以输出一个动作（只输出一个 JSON 对象，不要其他文字）：
1. 继续搜索：{"action": "search", "query": "你的检索词"}
   - 语料以英文为主（教务文档/培养方案/通知），部分中文；表格行多为"字段: 值"格式
   - 建议第一轮用问题的英文关键词，之后根据返回结果换同义词/换语言/换角度
2. 给出结论（任何时候都可以，最多搜索 {rounds} 轮后必须给）：
   {{"action": "verdict", "material_present": true 或 false, "confidence": 0.0-1.0,
     "evidence_quote": "能回答问题的原文片段（material_present 时必填，逐字摘录）",
     "reasoning": "一两句话依据"}}

判断标准：material_present = true 当且仅当存在一段原文，其中含有足以回答问题核心的信息
（不是"主题相关"而是"包含答案"）。问题问人数/容量时，只有原文表头或字段明确写出 capacity、人数上限等语义时，括号数字才可作为容量；单独的括号数字或课程行中的 numbers 只能作为待核实的标注，不能默认是容量上限。
"""


class CorpusSearcher:
    """Raw BM25 over one build — the agent's tool. No IR/governance: the agent
    judges MATERIAL EXISTENCE, not our retrieval pipeline."""

    def __init__(self, corpus, index):  # builder Corpus + BM25Index (duck-typed)
        self.corpus = corpus
        self.index = index
        self._titles = {uid: doc.get("title", "") for uid, doc in corpus.documents.items()}

    def search(self, query: str, k: int = TOP_K) -> list[dict[str, Any]]:
        hits = self.index.search(query, k)
        out = []
        for unit_index, score in hits:
            unit = self.index.units[unit_index]
            out.append({
                "doc_title": self._titles.get(unit.doc_uid, "")[:60],
                # Keep the complete identifier so exploratory material
                # probes remain auditable and duplicate documents cannot
                # collide after truncation.
                "doc_uid": unit.doc_uid,
                "score": round(float(score), 2),
                "text": (unit.text or "")[:400],
            })
        return out


def run_agent(question: str, searcher: CorpusSearcher, gateway: Gateway, *, max_rounds: int = MAX_ROUNDS) -> dict[str, Any]:
    """Run the search loop; returns {material_present, confidence, evidence_quote, reasoning, searches, rounds}."""
    messages: list[dict[str, str]] = [
        {"role": "system", "content": SYSTEM_PROMPT.replace("{rounds}", str(max_rounds))},
        {"role": "user", "content": f"待核查问题：{question}\n\n开始第一轮动作。"},
    ]
    searches: list[str] = []
    for round_no in range(1, max_rounds + 1):
        reply = gateway.chat(messages, max_tokens=800)
        try:
            action = extract_json(reply)
        except ValueError:
            messages.append({"role": "assistant", "content": reply[:400]})
            messages.append({"role": "user", "content": "输出不是合法 JSON 动作，请重新输出一个动作对象。"})
            continue
        if not isinstance(action, dict):
            continue
        if action.get("action") == "search" and action.get("query"):
            query = str(action["query"])[:200]
            searches.append(query)
            results = searcher.search(query)
            rendered = "\n\n".join(
                f"[{i}] 《{r['doc_title']}》 score={r['score']}\n{r['text']}" for i, r in enumerate(results, 1)
            ) or "（无结果）"
            messages.append({"role": "assistant", "content": reply})
            messages.append({"role": "user", "content": f"第 {round_no} 轮搜索结果（query={query!r}，top {len(results)}）：\n{rendered}\n\n继续下一轮动作。"})
            continue
        if action.get("action") == "verdict":
            return {
                "material_present": bool(action.get("material_present")),
                "confidence": round(float(action.get("confidence", 0.5)), 2),
                "evidence_quote": str(action.get("evidence_quote") or "")[:300],
                "reasoning": str(action.get("reasoning") or "")[:300],
                "searches": searches,
                "rounds": round_no,
            }
    # budget exhausted without a verdict — force one
    messages.append({"role": "user", "content": "搜索轮次已用完，现在必须输出 verdict 动作。"})
    reply = gateway.chat(messages, max_tokens=500)
    try:
        action = extract_json(reply)
    except ValueError:
        action = {"material_present": False, "confidence": 0.0, "reasoning": "agent produced no verdict"}
    return {
        "material_present": bool(action.get("material_present")),
        "confidence": round(float(action.get("confidence", 0.0)), 2),
        "evidence_quote": str(action.get("evidence_quote") or "")[:300],
        "reasoning": str(action.get("reasoning") or "")[:300],
        "searches": searches,
        "rounds": max_rounds,
    }
