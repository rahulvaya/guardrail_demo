"""Smoke test for the Presidio-backed `pii-detect` guard.

Runs the guard against a handful of representative inputs and prints the
decision + reasons + sanitized text. Intended for ad-hoc verification —
not part of the production test suite.

Usage (from guardrails-service/):
    .\\.venv-test\\Scripts\\python.exe scripts\\test_pii_presidio.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Make `app.*` importable when running from the repo root.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Importing the guards package self-registers all guards.
from app.core import guards as _guards  # noqa: F401, E402
from app.core.registry import build_guard  # noqa: E402


CASES: list[tuple[str, str]] = [
    ("benign",
     "Hi, what's my account balance for this month?"),
    ("email",
     "Please send the report to alice.smith@example.com"),
    ("ssn",
     "My SSN is 457-55-5462, can you look me up?"),
    ("credit-card",
     "Charge my Visa 4111 1111 1111 1111 for the transfer."),
    ("phone",
     "Call me at (415) 555-0132 if you need anything."),
    ("ip",
     "The login came from 192.168.10.42 last night."),
    ("iban",
     "Wire it to GB29 NWBK 6016 1331 9268 19 please."),
    ("multi",
     "I'm John Doe, email john@x.com, SSN 457-55-5462, "
     "card 5500 0000 0000 0004."),
]


async def run(mode: str) -> None:
    cfg = {"engine": "presidio", "mode": mode, "min_score": 0.3,
           "spacy_model": "en_core_web_sm",
           "entities": [
               "EMAIL_ADDRESS", "PHONE_NUMBER", "CREDIT_CARD",
               "US_SSN", "IP_ADDRESS", "IBAN_CODE", "CRYPTO",
           ]}
    guard = build_guard("pii-detect", cfg)
    print(f"\n=== mode={mode} | engine={getattr(guard, 'engine', '?')} ===")
    for label, text in CASES:
        result = await guard.check(text)
        out = {
            "case":      label,
            "decision":  result.decision.value if hasattr(result.decision, "value") else str(result.decision),
            "reasons":   result.reasons,
            "categories": result.categories,
            "sanitized": result.sanitized_text,
            "input":     text,
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))


async def main() -> int:
    await run("sanitize")
    await run("block")
    # Confirm regex fallback still works when Presidio is disabled.
    await run_regex()
    return 0


async def run_regex() -> None:
    cfg = {"engine": "regex", "mode": "sanitize"}
    guard = build_guard("pii-detect", cfg)
    print(f"\n=== mode=sanitize | engine={getattr(guard, 'engine', '?')} (fallback) ===")
    for label, text in CASES[:4]:
        result = await guard.check(text)
        print(json.dumps({
            "case":      label,
            "decision":  result.decision.value if hasattr(result.decision, "value") else str(result.decision),
            "reasons":   result.reasons,
            "sanitized": result.sanitized_text,
        }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
