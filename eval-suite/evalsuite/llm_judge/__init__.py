"""LLM-driven evaluation with source-bound reference and answer scoring.

The supported ``run`` path uses the reviewed question set's gold/evidence and
the product's final EvidencePack, claims and citation map.  The optional
``agent`` helper remains available for exploratory corpus-material probes, but
is not silently used to override a source-reviewed benchmark row.

``scoring`` multi-dimensional 1-5 scoring of the actual system answer
            (correctness / completeness / groundedness / refusal
            appropriateness / clarity / citation quality) with a verdict label,
            structured requirement coverage and rationale, judged against the
            reviewed reference plus the actual evidence and claim citations.
"""
