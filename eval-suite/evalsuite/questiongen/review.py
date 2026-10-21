"""P4: independent second-pass adjudication of generated questions.

Fresh context, different role (reviewer not author), same provider.  Output
is a review file in the eval.verify format (``[{"id", "decision"}]`` with
``decision`` ∈ approve/fix/reject) plus a reasons sidecar for audit.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evalsuite.questiongen import prompts
from evalsuite.questiongen.provider import QuestionGenProvider

BATCH = 20


def review_rows(rows: list[dict[str, Any]], provider: QuestionGenProvider, *, batch: int = BATCH) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    decisions: list[dict[str, Any]] = []
    reasons: list[dict[str, Any]] = []
    for start in range(0, len(rows), batch):
        chunk = rows[start:start + batch]
        try:
            raw = provider.generate_json(prompts.REVIEW_PROMPT.replace("{items}", prompts.review_items_block(chunk)), task="qg-review", max_tokens=3000)
        except Exception as error:
            for row in chunk:
                decisions.append({"id": row["id"], "decision": "fix"})  # conservative: human looks at it
                reasons.append({"id": row["id"], "reviewer_error": str(error)[:200]})
            continue
        by_id = {item.get("id"): item for item in raw if isinstance(item, dict)}
        for row in chunk:
            verdict = by_id.get(row["id"], {})
            decision = verdict.get("decision")
            if decision not in ("approve", "fix", "reject"):
                decision = "fix"
            decisions.append({"id": row["id"], "decision": decision})
            reasons.append({"id": row["id"], "decision": decision, "reasons": verdict.get("reasons", [])[:4]})
    return decisions, reasons


def write_review(decisions: list[dict[str, Any]], reasons: list[dict[str, Any]], out_dir: Path, *, stamp: str) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    review_path = out_dir / f"review_{stamp}.json"
    reasons_path = out_dir / f"review_{stamp}_reasons.json"
    review_path.write_text(json.dumps(decisions, ensure_ascii=False, indent=1), encoding="utf-8")
    reasons_path.write_text(json.dumps(reasons, ensure_ascii=False, indent=1), encoding="utf-8")
    return review_path, reasons_path
