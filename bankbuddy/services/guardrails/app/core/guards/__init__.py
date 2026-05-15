"""Built-in guards. Importing this package registers all guards."""
from . import (
    azure_content_safety,     # noqa: F401  (Azure AI Content Safety - default)
    azure_pii_detection,      # noqa: F401  (Azure AI Language PII)
    banking_relevance,        # noqa: F401  (custom example)
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
    toxicity,                 # noqa: F401
)
