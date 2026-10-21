"""Render a readable Markdown report from editorial_review.jsonl."""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path


def load(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def md(text):
    return str(text or "").replace("|", "\\|").replace("\n", " ").strip()


def safe_answer(row):
    if row["decision"] in {"separate_synthetic_safety"} or row["id"] in {"sm-0115", "sm-0116", "sm-0117", "sm-0118"}:
        return "[已移出正式报告：含个人申请者信息或合成安全情境]"
    return row.get("revised_answer", "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", type=Path, required=True)
    ap.add_argument("--summary", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    rows = load(args.ledger)
    summary = json.loads(args.summary.read_text(encoding="utf-8"))
    groups = defaultdict(list)
    for r in rows:
        groups[r["original_level"]].append(r)
    lines = [
        "# 五层评测题集人工源审稿报告（editorial-v1-20261015）",
        "",
        "> 本报告是 Codex 按当前 corpus build 逐题阅读问题、标准答案和证据块后的源审稿记录。`reviewer=codex_source_review`，`human_approved=false`；它可以作为开发版题集和复核底稿，不能表述为用户或人类已经批准。",
        "",
        "## 审稿边界",
        "",
        f"- 原题数：{summary['n_original']}；正式修订开发集：{summary['n_revised']}；隔离待确认：{summary['n_quarantine']}；独立轨道：{summary['n_separate']}。",
        f"- 原题 SHA-256：`{summary['source_questions_sha256']}`。语料 build：`{summary['corpus_build']}`。修订题集 SHA-256：`{summary['revised_sha256']}`。",
        "- 证据选择优先使用原题 `source_doc`；重复文档、空 `block_id`、错届别和无法在当前 build 找到的引用不沿用。",
        "- 括号数字只有在原文明确出现 `students` 等字段时才解释为人数；没有图例的表格容量题隔离。",
        "- C4 必须有两个独立且必要的前提；目录标题、单块直接规则和臆测后果不再凑成多跳。",
        "- C5 区分正规例外、合法咨询、代理提交、绕过系统、隐私和成绩篡改；原文没有禁止时不编造拒答理由。",
        "- C1 另存为边界探针：空 gold 不能证明整个语料不包含常识答案。",
        "",
        "## 裁决统计",
        "",
        "| 裁决 | 数量 |",
        "| --- | ---: |",
    ]
    for k, v in summary["decisions"].items():
        lines.append(f"| {k} | {v} |")
    lines += ["", "| 修订集层级 | 数量 |", "| --- | ---: |"]
    for k, v in summary["levels_revised"].items():
        lines.append(f"| {k} | {v} |")
    lines += ["", "## 逐题台账", ""]
    for level in ["C1", "C2", "C3", "C4", "C5"]:
        lines += [f"### {level}", "", "| ID | 裁决 | 新层级 | 原题（摘要） | 修订题 | 修订答案/处理 | 证据块 |", "| --- | --- | --- | --- | --- | --- | --- |"]
        for r in groups[level]:
            evidence = ", ".join(g.get("block_id", "") for g in r.get("revised_gold", [])) or "—"
            original = r["original_question"]
            if r["decision"] in {"separate_synthetic_safety"} or r["id"] in {"sm-0115", "sm-0116", "sm-0117", "sm-0118"}:
                original = "[个人/合成敏感情境，已移出]"
            lines.append("| " + " | ".join([
                r["id"], r["decision"], r.get("reviewed_level", r["original_level"]),
                md(original), md(r["revised_question"]), md(safe_answer(r)), md(evidence)
            ]) + " |")
            lines.append(f"\n> {r['id']} 理由：{md(r['reason'])}\n")
        lines.append("")
    lines += ["## 文件说明", "", "- `revised_benchmark.jsonl`：有当前 build 证据块、可用于开发诊断的修订题集；尚未获得用户人工批准。", "- `editorial_review.jsonl`：251 行逐题裁决台账，保留原题摘要、修订题、修订答案、证据和理由。", "- `quarantine.jsonl`：暂不进入正式评分的题，需补原表、澄清对象/年份、修正来源或重写为合成测试。", "- `separate_tracks.jsonl`：C1 边界探针、历史材料、个人申请者数据和合成安全题等独立轨道。", "", "评测比较时请使用新版本的题集、作答和 judge manifest；原 251 题首评结果不能与本修订集直接横比。"]
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(args.out)


if __name__ == "__main__":
    main()
