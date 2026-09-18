"""Reddit backlink endpoints: subreddit discovery, rules, draft generation, publish, history.

HTTP surface only. The two rules that shape this feature - review before publish,
and local guardrails before Reddit's word - live in
services/backlinks/backlink_service.py.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from controller import backlinks as backlinks_controller
from db.session import get_session
from schemas import (
    PublishResult,
    RedditDraftRequest,
    RedditDraftResponse,
    RedditPostsResponse,
    RedditPublishRequest,
    SubredditRulesResponse,
    SubredditsResponse,
)

router = APIRouter(prefix="/api/backlinks/reddit", tags=["backlinks"])


# --- Subreddits ------------------------------------------------------------


@router.get("/subreddits", response_model=SubredditsResponse)
async def list_subreddits(request: Request) -> SubredditsResponse:
    """Subreddits this account moderates (first) and subscribes to."""
    return await backlinks_controller.list_subreddits(request.session.get("user_id"))


@router.get("/subreddits/{name}/rules", response_model=SubredditRulesResponse)
async def subreddit_rules(request: Request, name: str) -> SubredditRulesResponse:
    """Rules + link flairs for one subreddit. Cached for an hour."""
    return await backlinks_controller.subreddit_rules(request.session.get("user_id"), name)


# --- Drafts ----------------------------------------------------------------


@router.post("/drafts", response_model=RedditDraftResponse)
async def create_draft(
    request: Request,
    payload: RedditDraftRequest,
    session: AsyncSession = Depends(get_session),
) -> RedditDraftResponse:
    """Profile the store, read the subreddit's rules, and draft a post with Gemini.

    Synchronous on purpose: the user is waiting on the result and will review it.
    """
    return await backlinks_controller.create_draft(
        request.session.get("user_id"), payload, session
    )


@router.post("/drafts/{draft_id}/publish", response_model=PublishResult)
async def publish_draft(
    request: Request,
    draft_id: str,
    payload: RedditPublishRequest,
    session: AsyncSession = Depends(get_session),
) -> PublishResult:
    """Publish one draft, applying the reviewer's edits."""
    return await backlinks_controller.publish_draft(
        request.session.get("user_id"), draft_id, payload, session
    )


# --- History ---------------------------------------------------------------


@router.get("/posts", response_model=RedditPostsResponse)
async def list_posts(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> RedditPostsResponse:
    """Every draft and publish for this session, newest first."""
    return await backlinks_controller.list_posts(request.session.get("user_id"), limit, session)
