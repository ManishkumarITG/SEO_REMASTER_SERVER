"""Crawler/fetcher errors.

Moved here verbatim from services/crawler/fetcher.py during the layered-architecture
reorganisation. Class names, bases, messages and __init__ behaviour are
unchanged, so `isinstance` checks and `except` clauses behave exactly as before.
"""

from __future__ import annotations


class FetchError(Exception):
    """Raised when a page cannot be fetched or is not auditable HTML."""
