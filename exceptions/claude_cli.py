"""Claude Code CLI gateway errors.

Mirrors exceptions/gemini.py and exceptions/ollama.py one-for-one so the report
routes map every engine the same way: NotConfigured -> 503, everything else -> 502.
"""

from __future__ import annotations


class ClaudeCliError(Exception):
    """Base error for CLI failures: not logged in, usage limit, crash, timeout."""


class ClaudeCliNotConfigured(ClaudeCliError):
    """Raised when the `claude` executable cannot be found. Routes turn this into a 503."""


class ClaudeCliResponseError(ClaudeCliError):
    """Raised when the CLI ran but returned nothing usable (no JSON, or an empty result)."""

    def __init__(self, message: str, raw: str = "") -> None:
        super().__init__(message)
        self.raw = raw
