"""Gemini gateway errors.

Moved here verbatim from services/integrations/gemini.py during the layered-architecture
reorganisation. Class names, bases, messages and __init__ behaviour are
unchanged, so `isinstance` checks and `except` clauses behave exactly as before.
"""

from __future__ import annotations


class GeminiError(Exception):
    """Base error for Gemini failures."""


class GeminiNotConfigured(GeminiError):
    """Raised when GEMINI_API_KEY is unset. Routes turn this into a 503."""


class GeminiResponseError(GeminiError):
    """Raised when the model never returned JSON matching the requested schema."""

    def __init__(self, message: str, raw: str = "") -> None:
        super().__init__(message)
        self.raw = raw
