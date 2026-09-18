"""Backlink pipeline errors.

Moved here verbatim from services/backlinks/{guardrails,reddit_post_generator}.py during the layered-architecture
reorganisation. Class names, bases, messages and __init__ behaviour are
unchanged, so `isinstance` checks and `except` clauses behave exactly as before.
"""

from __future__ import annotations


class GuardrailError(Exception):
    """A local guardrail blocked the publish. `status_code` is the HTTP answer to give."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class DraftValidationError(Exception):
    """The generated draft breaks a hard limit. Surfaced to the UI, never truncated."""
