"""Built-in guards. Importing this package registers all guards."""
from . import (
    azure_content_safety,     # noqa: F401  (Azure AI Content Safety - default)
    azure_groundedness,       # noqa: F401  (Azure managed groundedness)
    azure_pii_detection,      # noqa: F401  (Azure AI Language PII)
    azure_task_adherence,     # noqa: F401  (Azure managed task adherence)
    banned_substrings,        # noqa: F401
    bias_detect,              # noqa: F401  (stereotype / demographic skew)
    competitor_mentions,      # noqa: F401
    groundedness,             # noqa: F401  (RAG hallucination check)
    output_pii_redact,        # noqa: F401
    pii_detect,               # noqa: F401
    prompt_injection,         # noqa: F401
    secret_leak,              # noqa: F401
    task_adherence,           # noqa: F401  (runtime task-scope enforcement)
    token_limit,              # noqa: F401
    topic_relevance,          # noqa: F401  (custom-guard reference impl)
    toxicity,                 # noqa: F401
)
