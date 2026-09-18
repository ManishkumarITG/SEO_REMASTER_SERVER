"""Reddit backlink controller: orchestration only."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from schemas import (
    PublishResult,
    RedditDraftRequest,
    RedditDraftResponse,
    RedditPostsResponse,
    RedditPublishRequest,
    SubredditRulesResponse,
    SubredditsResponse,
)
from services.backlinks import backlink_service


async def list_subreddits(user_id: str | None) -> SubredditsResponse:
    return await backlink_service.list_subreddits(user_id)


async def subreddit_rules(user_id: str | None, name: str) -> SubredditRulesResponse:
    return await backlink_service.subreddit_rules(user_id, name)


async def create_draft(
    user_id: str | None, payload: RedditDraftRequest, session: AsyncSession
) -> RedditDraftResponse:
    return await backlink_service.create_draft(user_id, payload, session)


async def publish_draft(
    user_id: str | None, draft_id: str, payload: RedditPublishRequest, session: AsyncSession
) -> PublishResult:
    return await backlink_service.publish_draft(user_id, draft_id, payload, session)


async def list_posts(user_id: str | None, limit: int, session: AsyncSession) -> RedditPostsResponse:
    return await backlink_service.list_posts(user_id, limit, session)
