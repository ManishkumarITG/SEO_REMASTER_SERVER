"""Domain exception types, re-exported flat.

These classes used to live beside the modules that raise them. They are
unchanged - same names, same bases, same attributes - only relocated, so
`except RedditRateLimited` and `isinstance(exc, GeminiError)` still mean
exactly what they meant before.
"""

from exceptions.backlinks import DraftValidationError, GuardrailError
from exceptions.crawler import FetchError
from exceptions.gemini import GeminiError, GeminiNotConfigured, GeminiResponseError
from exceptions.llm import LLMError, LLMJSONError, LLMNotConfiguredError
from exceptions.reddit import (
    RedditAPIError,
    RedditAuthError,
    RedditError,
    RedditNotConfigured,
    RedditNotConnected,
    RedditRateLimited,
)

__all__ = [
    "DraftValidationError",
    "FetchError",
    "GeminiError",
    "GeminiNotConfigured",
    "GeminiResponseError",
    "GuardrailError",
    "LLMError",
    "LLMJSONError",
    "LLMNotConfiguredError",
    "RedditAPIError",
    "RedditAuthError",
    "RedditError",
    "RedditNotConfigured",
    "RedditNotConnected",
    "RedditRateLimited",
]
