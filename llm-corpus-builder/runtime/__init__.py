"""Query-side runtime (Stage 3A): CorpusEngine, its HTTP façade and the frozen
answer-context contract shared by the eval harness and the product layer."""

from runtime.contract import ANSWER_CONTEXT_VERSION, DEFAULT_CONFIG, SERVICE_CONFIGS, validate_answer_context
from runtime.engine import CorpusEngine

__all__ = ["ANSWER_CONTEXT_VERSION", "DEFAULT_CONFIG", "SERVICE_CONFIGS", "CorpusEngine", "validate_answer_context"]
