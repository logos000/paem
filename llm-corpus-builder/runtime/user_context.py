"""Merge explicit user context into the question's QueryIR (Stage 3A).

Rule R1: **an explicit statement in the question always wins; user context
only fills blanks.**  "我是研究生，2027 春有什么补贴？" keeps the in-question
term and cohort even if the profile says otherwise — the words a user typed
right now express current intent better than any stored profile.  Course codes
are additive identifiers rather than constraints, so they union instead.

The merge is auditable: every field the user context actually contributed is
listed in ``filled`` and echoed in the payload; ignored/unknown keys never
reach the pack.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from evidence.query_ir import QueryIR

#: Keys the runtime understands.  Unknown keys are dropped (logged in ``filled``
#: as ``ignored:<key>`` so callers can discover typos instead of losing data
#: silently).
USER_CONTEXT_KEYS = ("audience", "cohort", "term", "academic_year", "program", "course_codes")

#: Keys the evidence-pack context contract consumes (see evidence.pack._conflicts).
PACK_CONTEXT_KEYS = ("program", "cohort", "term")


def _as_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    value = str(value or "").strip()
    return [value] if value else []


def merge_user_context(ir: QueryIR, user_context: dict[str, Any] | None) -> tuple[QueryIR, dict[str, str], list[str]]:
    """Return ``(merged_ir, pack_context, filled)``.

    ``pack_context`` carries only the user-provided program/cohort/term values
    (stringified, as the pack contract expects); question-derived constraints
    already flow through the merged IR, so keeping them out of the pack context
    preserves exact parity with the question-only path.
    """
    user_context = user_context or {}
    filled: list[str] = []
    updates: dict[str, Any] = {}

    term = _as_list(user_context.get("term"))
    if term and not ir.terms:
        updates["terms"] = term
        filled.append("term")
    audience = str(user_context.get("audience") or "").strip()
    if audience and not ir.audience:
        updates["audience"] = audience
        filled.append("audience")
    cohorts = _as_list(user_context.get("cohort"))
    if cohorts and not ir.cohorts:
        updates["cohorts"] = cohorts
        filled.append("cohort")
    academic_year = _as_list(user_context.get("academic_year"))
    if academic_year and not ir.academic_years:
        updates["academic_years"] = academic_year
        filled.append("academic_year")
    course_codes = _as_list(user_context.get("course_codes"))
    if course_codes:
        merged_codes = list(dict.fromkeys([str(code) for code in (ir.course_codes + course_codes)]))
        if merged_codes != ir.course_codes:
            updates["course_codes"] = merged_codes
            filled.append("course_codes")

    for key in user_context:
        if key not in USER_CONTEXT_KEYS:
            filled.append(f"ignored:{key}")

    merged = dataclasses.replace(ir, **updates) if updates else ir

    pack_context: dict[str, str] = {}
    for key in PACK_CONTEXT_KEYS:
        if key not in user_context:
            continue
        if key == "program":
            value = str(user_context.get(key) or "").strip()
        else:
            # cohort/term may arrive as scalars or lists; the pack stringifies.
            value = ", ".join(_as_list(user_context.get(key)))
        if value:
            pack_context[key] = value
    return merged, pack_context, filled
