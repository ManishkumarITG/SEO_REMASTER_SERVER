"""Volume guardrails, enforced BEFORE anything is sent to Reddit.

These are what keep a connected account alive and the app inside Reddit's terms:
a daily cap, a minimum gap between posts, and a store+subreddit dedupe window.
All three are config-driven (REDDIT_MAX_POSTS_PER_DAY, REDDIT_MIN_MINUTES_BETWEEN_POSTS,
REDDIT_DEDUPE_DAYS) and all three are computed from published rows in reddit_posts.

Reddit's OWN limits are never pre-empted here - when Reddit rate-limits a submit we
surface its message verbatim and never retry. This module only stops us from being
the reason an account gets banned.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings
from exceptions.backlinks import GuardrailError
from models.db import RedditPost


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def check_publish_allowed(
    session: AsyncSession, user_id: str, store_url: str, subreddit: str
) -> None:
    """Raise GuardrailError if this publish would break a local guardrail.

    429 for the daily cap and the minimum gap, 409 for the dedupe window.
    """
    settings = get_settings()
    now = datetime.now(UTC)

    published = RedditPost.status == "published"

    # 1. Daily cap per connected account.
    day_ago = now - timedelta(days=1)
    used = await session.scalar(
        select(func.count())
        .select_from(RedditPost)
        .where(RedditPost.user_id == user_id, published, RedditPost.published_at >= day_ago)
    )
    if (used or 0) >= settings.reddit_max_posts_per_day:
        raise GuardrailError(
            429,
            f"Daily limit reached: {used} of {settings.reddit_max_posts_per_day} posts published "
            "in the last 24 hours. This cap protects the connected account - raise "
            "REDDIT_MAX_POSTS_PER_DAY only if you know the account can carry it.",
        )

    # 2. Minimum gap between posts.
    last_published = await session.scalar(
        select(func.max(RedditPost.published_at)).where(RedditPost.user_id == user_id, published)
    )
    last_published = _as_utc(last_published)
    if last_published is not None:
        gap = timedelta(minutes=settings.reddit_min_minutes_between_posts)
        earliest = last_published + gap
        if now < earliest:
            wait_minutes = max(1, int((earliest - now).total_seconds() // 60) + 1)
            raise GuardrailError(
                429,
                f"Too soon: posts are spaced at least "
                f"{settings.reddit_min_minutes_between_posts} minutes apart. "
                f"Try again in about {wait_minutes} minute(s).",
            )

    # 3. Dedupe: same store + same subreddit inside the window.
    window_start = now - timedelta(days=settings.reddit_dedupe_days)
    duplicate = await session.scalar(
        select(RedditPost.permalink).where(
            RedditPost.user_id == user_id,
            published,
            RedditPost.store_url == store_url,
            func.lower(RedditPost.subreddit) == subreddit.lower(),
            RedditPost.published_at >= window_start,
        )
    )
    if duplicate is not None:
        raise GuardrailError(
            409,
            f"This store was already posted to r/{subreddit} within the last "
            f"{settings.reddit_dedupe_days} days ({duplicate}). Reposting the same shop to "
            "the same subreddit reads as spam.",
        )
