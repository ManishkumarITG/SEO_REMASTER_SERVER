"""Ollama gateway errors.

Mirrors exceptions/gemini.py one-for-one so the report routes can map both
engines the same way: NotConfigured -> 503, everything else -> 502.
"""

from __future__ import annotations


class OllamaError(Exception):
    """Base error for Ollama failures: unreachable, model not pulled, bad reply."""


class OllamaNotConfigured(OllamaError):
    """Raised when OLLAMA_BASE_URL is blank. Routes turn this into a 503."""


class OllamaResponseError(OllamaError):
    """Raised when Ollama answered but with nothing usable (empty, or not JSON)."""

    def __init__(self, message: str, raw: str = "") -> None:
        super().__init__(message)
        self.raw = raw
