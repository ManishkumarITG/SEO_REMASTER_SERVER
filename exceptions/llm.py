"""LLM (Anthropic) gateway errors.

Moved here verbatim from services/integrations/llm.py during the layered-architecture
reorganisation. Class names, bases, messages and __init__ behaviour are
unchanged, so `isinstance` checks and `except` clauses behave exactly as before.
"""

from __future__ import annotations


class LLMError(Exception):
    """Base error for LLM failures."""


class LLMNotConfiguredError(LLMError):
    """Raised when no API key is configured."""


class LLMJSONError(LLMError):
    """Raised when json_mode=True but the model did not return valid JSON."""

    def __init__(self, message: str, raw: str) -> None:
        super().__init__(message)
        self.raw = raw
