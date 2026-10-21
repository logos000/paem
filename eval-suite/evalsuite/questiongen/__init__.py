"""Question-set generation sub-project (2026-10-15, user request).

Multi-layer / multi-language / multi-intent / multi-tone evaluation questions
generated per page plus cross-page combination questions, with gold anchors
assigned BY CONSTRUCTION (the source block is the anchor).  Pipeline:

    P1 packets  — every active document becomes a material packet, plus
                  series-pair and conflict-pair combination packets
    P2 generate — matrix-driven drafting through the campus model gateway
                  (or any OpenAI-compatible endpoint; SP red line: the
                  gateway choice is a user ruling, recorded in OPERATIONS)
    P3 lint     — eval.lint gate + new-field validation (language/tone/
                  layer/intent/family) and cross-set dedup
    P4 review   — independent second-pass adjudication (same provider,
                  fresh context, different prompt)
    P5 verify   — the existing eval.verify gen/write + mark_provenance flow
    P6 report   — dimension breakdowns + consistency@family

New rows stay in their OWN eval file (never merged into the frozen
verified_eval_v3) — the frozen discipline is untouched.
"""
