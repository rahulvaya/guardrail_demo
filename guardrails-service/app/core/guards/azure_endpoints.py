"""Single source of truth for every Azure URL the guardrails call.

All Azure-backed guards (``azure-content-safety``, ``azure-pii-detection``)
build their request URLs from the helpers in this module. If you want to
audit *which* Azure REST endpoints the guardrail layer talks to, this is
the only file you need to read.

Endpoints (path + REST docs):

    Azure AI Content Safety
      POST {endpoint}/contentsafety/text:shieldPrompt   -- Prompt Shields
        https://learn.microsoft.com/azure/ai-services/content-safety/quickstart-jailbreak
      POST {endpoint}/contentsafety/text:analyze        -- Harm categories
        https://learn.microsoft.com/azure/ai-services/content-safety/quickstart-text

    Azure AI Language
      POST {endpoint}/language/:analyze-text            -- PII / NER / etc.
        https://learn.microsoft.com/azure/ai-services/language-service/personally-identifiable-information/overview

    Auth (shared)
      AAD scope: https://cognitiveservices.azure.com/.default
        Same scope works for both APIs when exposed by a multi-service
        Azure AI Services / Cognitive Services resource.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Default API versions (override per-guard via config if you need a newer one)
# ---------------------------------------------------------------------------

CONTENT_SAFETY_API_VERSION = "2024-09-01"
LANGUAGE_API_VERSION = "2023-04-01"

# ---------------------------------------------------------------------------
# Shared AAD scope for Cognitive Services / Azure AI Services
# ---------------------------------------------------------------------------

COGNITIVE_SERVICES_AAD_SCOPE = "https://cognitiveservices.azure.com/.default"

# ---------------------------------------------------------------------------
# URL path templates (relative to the resource endpoint)
# ---------------------------------------------------------------------------

CONTENT_SAFETY_SHIELD_PROMPT_PATH = "/contentsafety/text:shieldPrompt"
CONTENT_SAFETY_TEXT_ANALYZE_PATH = "/contentsafety/text:analyze"
CONTENT_SAFETY_PROTECTED_MATERIAL_PATH = "/contentsafety/text:detectProtectedMaterial"
CONTENT_SAFETY_GROUNDEDNESS_PATH = "/contentsafety/text:detectGroundedness"
CONTENT_SAFETY_TASK_ADHERENCE_PATH = "/contentsafety/text:detectTaskAdherence"
CONTENT_SAFETY_CUSTOM_CATEGORY_PATH = "/contentsafety/text:analyzeCustomCategory"
LANGUAGE_ANALYZE_TEXT_PATH = "/language/:analyze-text"


def _normalize(endpoint: str) -> str:
    """Strip trailing slash so path concatenation never doubles up."""
    return (endpoint or "").rstrip("/")


# ---------------------------------------------------------------------------
# URL builders -- the only functions guards should call to obtain a URL
# ---------------------------------------------------------------------------

def content_safety_shield_prompt_url(
    endpoint: str, api_version: str = CONTENT_SAFETY_API_VERSION
) -> str:
    """Prompt Shields: jailbreak / prompt-injection detection (INPUT only)."""
    return (
        f"{_normalize(endpoint)}{CONTENT_SAFETY_SHIELD_PROMPT_PATH}"
        f"?api-version={api_version}"
    )


def content_safety_text_analyze_url(
    endpoint: str, api_version: str = CONTENT_SAFETY_API_VERSION
) -> str:
    """Harm categories: Hate / SelfHarm / Sexual / Violence (INPUT + OUTPUT)."""
    return (
        f"{_normalize(endpoint)}{CONTENT_SAFETY_TEXT_ANALYZE_PATH}"
        f"?api-version={api_version}"
    )


def content_safety_protected_material_url(
    endpoint: str, api_version: str = CONTENT_SAFETY_API_VERSION
) -> str:
    """Protected Material detection: copyrighted text (OUTPUT).

    Docs: https://learn.microsoft.com/azure/ai-services/content-safety/quickstart-protected-material
    """
    return (
        f"{_normalize(endpoint)}{CONTENT_SAFETY_PROTECTED_MATERIAL_PATH}"
        f"?api-version={api_version}"
    )


def content_safety_groundedness_url(
    endpoint: str, api_version: str = CONTENT_SAFETY_API_VERSION
) -> str:
    """Azure managed Groundedness detection (OUTPUT, requires sources).

    Docs: https://learn.microsoft.com/azure/ai-services/content-safety/quickstart-groundedness
    """
    return (
        f"{_normalize(endpoint)}{CONTENT_SAFETY_GROUNDEDNESS_PATH}"
        f"?api-version={api_version}"
    )


def content_safety_task_adherence_url(
    endpoint: str, api_version: str = CONTENT_SAFETY_API_VERSION
) -> str:
    """Azure managed Task Adherence detection (OUTPUT, agent task scope).

    Docs: https://learn.microsoft.com/azure/ai-services/content-safety/concepts/task-adherence
    """
    return (
        f"{_normalize(endpoint)}{CONTENT_SAFETY_TASK_ADHERENCE_PATH}"
        f"?api-version={api_version}"
    )


def content_safety_custom_category_url(
    endpoint: str,
    api_version: str = CONTENT_SAFETY_API_VERSION,
    path: str = CONTENT_SAFETY_CUSTOM_CATEGORY_PATH,
) -> str:
    """Azure Custom Categories Standard inference (INPUT scope filter).

    Single-category-per-call: callers fan out over their configured
    category list. The ``path`` arg lets policy authors point at the
    future Rapid preview path (or any other inline-definition variant)
    without code changes.

    Docs: https://learn.microsoft.com/azure/ai-services/content-safety/concepts/custom-categories
    """
    return (
        f"{_normalize(endpoint)}{path}"
        f"?api-version={api_version}"
    )


def language_analyze_text_url(
    endpoint: str, api_version: str = LANGUAGE_API_VERSION
) -> str:
    """Azure AI Language ``:analyze-text`` (PII Entity Recognition, NER, ...)."""
    return (
        f"{_normalize(endpoint)}{LANGUAGE_ANALYZE_TEXT_PATH}"
        f"?api-version={api_version}"
    )
