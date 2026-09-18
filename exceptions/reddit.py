"""Reddit domain errors.

reddit/client.py deliberately raises these rather than HTTPException: the HTTP
mapping lives in one place, controller/backlinks.py::_http_for, because Reddit's
own error text must reach the UI verbatim.

Moved here verbatim from services/integrations/reddit.py during the layered-architecture
reorganisation. Class names, bases, messages and __init__ behaviour are
unchanged, so `isinstance` checks and `except` clauses behave exactly as before.
"""

from __future__ import annotations


class RedditError(Exception):
    """Base error for every Reddit failure."""


class RedditNotConfigured(RedditError):
    """REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET are unset. Routes answer 503."""


class RedditNotConnected(RedditError):
    """This user has no stored Reddit credential. Routes answer 401."""


class RedditAuthError(RedditError):
    """The stored token is invalid or was revoked. Routes answer 401."""


class RedditRateLimited(RedditError):
    """Reddit said no. The message is Reddit's own and is NEVER retried."""

    def __init__(self, message: str, reset_seconds: float | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.reset_seconds = reset_seconds


class RedditAPIError(RedditError):
    """Any other Reddit-side failure, carrying Reddit's error code and message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}" if code else message)
        self.code = code
        self.message = message
