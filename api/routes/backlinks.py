"""Reddit backlink endpoints: subreddit discovery, rules, draft generation, publish, history.

Two rules shape this file:

1. **Review before publish.** POST /drafts only generates; publishing is a second,
   explicit call. `auto_publish` exists for users who want the automatic flow, but it
   is opt-in per request and off by default in the UI.
2. **Local guardrails first, Reddit's word last.** The daily cap / minimum gap /
   dedupe checks run BEFORE any Reddit call. When Reddit itself refuses, its message
   is returned verbatim and nothing is retried.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import get_session
from models.db import RedditPost
from models.schemas import (
    LinkFlairOut,
    PublishResult,
    RedditDraftRequest,
    RedditDraftResponse,
    RedditPostsResponse,
    RedditPostSummary,
    RedditPublishRequest,
    StoreProfileSummary,
    SubredditRuleOut,
    SubredditRulesResponse,
    SubredditsResponse,
    SubredditSummary,
)
from services.backlinks.guardrails import GuardrailError, check_publish_allowed
from services.backlinks.reddit_post_generator import (
    BODY_MAX_CHARS,
    TITLE_MAX_CHARS,
    DraftValidationError,
    LinkFlair,
    PostAngle,
    RedditDraftSchema,
    SubredditContext,
    SubredditRule,
    generate_reddit_draft,
)
from services.backlinks.store_profile import StoreProfile, build_store_profile
from services.crawler.fetcher import FetchError
from services.integrations.gemini import (
    GeminiError,
    GeminiNotConfigured,
    get_gemini_service,
)
from services.integrations.reddit import (
    RedditAPIError,
    RedditAuthError,
    RedditClient,
    RedditError,
    RedditNotConfigured,
    RedditNotConnected,
    RedditRateLimited,
    get_client_for_user,
)

logger = logging.getLogger("seo.backlinks")

router = APIRouter(prefix="/api/backlinks/reddit", tags=["backlinks"])


def _require_user(request: Request) -> str:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Reddit is not connected for this session")
    return user_id


def _http_for(exc: RedditError) -> HTTPException:
    """Single place where Reddit domain errors become HTTP responses."""
    if isinstance(exc, RedditNotConfigured):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, RedditNotConnected):
        return HTTPException(status_code=401, detail="Reddit is not connected for this session")
    if isinstance(exc, RedditAuthError):
        return HTTPException(status_code=401, detail=str(exc))
    if isinstance(exc, RedditRateLimited):
        # Reddit's own message, unmodified, and never retried.
        return HTTPException(status_code=429, detail=exc.message)
    if isinstance(exc, RedditAPIError):
        return HTTPException(status_code=502, detail=exc.message)
    return HTTPException(status_code=502, detail="Reddit request failed")


async def _client(user_id: str) -> RedditClient:
    try:
        return await get_client_for_user(user_id)
    except RedditError as exc:
        raise _http_for(exc) from exc


def _profile_summary(profile: StoreProfile) -> StoreProfileSummary:
    return StoreProfileSummary(
        store_url=profile.store_url,
        store_name=profile.store_name,
        tagline=profile.tagline,
        is_shopify=profile.is_shopify,
        product_count=len(profile.products),
        categories=profile.categories,
        products_source=profile.products_source,
    )


def _summary(row: RedditPost) -> RedditPostSummary:
    return RedditPostSummary(
        draft_id=row.id,
        created_at=row.created_at,
        published_at=row.published_at,
        store_url=row.store_url,
        store_name=row.store_name,
        subreddit=row.subreddit,
        angle=row.angle,
        kind=row.kind,  # type: ignore[arg-type]
        title=row.title,
        status=row.status,  # type: ignore[arg-type]
        fit_score=row.fit_score,
        permalink=row.permalink,
        error_message=row.error_message,
        auto_published=row.auto_published,
    )


# --- Subreddits ------------------------------------------------------------


@router.get("/subreddits", response_model=SubredditsResponse)
async def list_subreddits(request: Request) -> SubredditsResponse:
    """Subreddits this account moderates (first) and subscribes to."""
    user_id = _require_user(request)
    client = await _client(user_id)
    try:
        moderated, subscribed = await client.get_my_subreddits()
    except RedditError as exc:
        raise _http_for(exc) from exc
    return SubredditsResponse(
        moderated=[SubredditSummary(**_sub_fields(sub)) for sub in moderated],
        subscribed=[SubredditSummary(**_sub_fields(sub)) for sub in subscribed],
    )


def _sub_fields(sub: dict[str, object]) -> dict[str, object]:
    return {
        "name": sub.get("name", ""),
        "title": sub.get("title", ""),
        "subscribers": sub.get("subscribers", 0),
        "you_moderate": sub.get("you_moderate", False),
    }


@router.get("/subreddits/{name}/rules", response_model=SubredditRulesResponse)
async def subreddit_rules(request: Request, name: str) -> SubredditRulesResponse:
    """Rules + link flairs for one subreddit. Cached for an hour."""
    user_id = _require_user(request)
    client = await _client(user_id)
    try:
        rules = await client.get_subreddit_rules(name)
        flairs = await client.get_link_flairs(name)
    except RedditError as exc:
        raise _http_for(exc) from exc
    return SubredditRulesResponse(
        subreddit=name,
        rules=[SubredditRuleOut(**_rule_fields(rule)) for rule in rules.get("rules", [])],
        flairs=[LinkFlairOut(id=flair["id"], text=flair["text"]) for flair in flairs],
        self_promo_warning=bool(rules.get("self_promo_warning")),
        public_description=str(rules.get("public_description") or ""),
    )


def _rule_fields(rule: dict[str, object]) -> dict[str, object]:
    return {
        "short_name": rule.get("short_name", ""),
        "description": rule.get("description", ""),
    }


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
    user_id = _require_user(request)
    gemini = get_gemini_service()
    if not gemini.is_configured:
        raise HTTPException(
            status_code=503,
            detail="Gemini is not configured - set GEMINI_API_KEY in app/server/.env",
        )
    client = await _client(user_id)

    try:
        rules = await client.get_subreddit_rules(payload.subreddit)
        flairs = await client.get_link_flairs(payload.subreddit)
    except RedditError as exc:
        raise _http_for(exc) from exc

    try:
        profile = await build_store_profile(payload.store_url)
    except FetchError as exc:
        raise HTTPException(status_code=422, detail=f"Could not read the store page: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    context = SubredditContext(
        name=payload.subreddit,
        title=str(rules.get("title") or ""),
        public_description=str(rules.get("public_description") or ""),
        rules=[SubredditRule(**_rule_fields(rule)) for rule in rules.get("rules", [])],
        flairs=[LinkFlair(id=flair["id"], text=flair["text"]) for flair in flairs],
    )

    try:
        draft = await generate_reddit_draft(
            profile, context, PostAngle(payload.angle), payload.extra_notes, service=gemini
        )
    except GeminiNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except DraftValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except GeminiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    flair_text = draft.suggested_flair_text
    flair_id = payload.flair_id or _flair_id_for(flairs, flair_text)
    row = RedditPost(
        user_id=user_id,
        store_url=profile.store_url,
        store_name=profile.store_name,
        subreddit=payload.subreddit,
        angle=payload.angle.value,
        kind=payload.kind,
        title=draft.title,
        body_markdown=draft.body_markdown,
        flair_id=flair_id,
        flair_text=flair_text,
        status="draft",
        fit_score=draft.fit_score,
        rule_check_notes=draft.rule_check_notes,
        gemini_model=gemini.model,
        auto_published=False,
    )
    session.add(row)
    await session.commit()

    response = _draft_response(row, draft, _profile_summary(profile))
    if payload.auto_publish:
        # The draft is already generated, paid for and stored. If publishing is
        # refused - by a local guardrail or by Reddit - the caller must still get
        # the draft back with the reason attached, so it can be reviewed and
        # published later. Raising here would strand a row the UI never saw.
        try:
            response.publish = await _publish(session, user_id, row, auto=True)
        except HTTPException as exc:
            row.status = "failed"
            row.error_message = str(exc.detail)
            await session.commit()
            logger.info("Auto-publish blocked for draft %s: %s", row.id, row.error_message)
            response.publish = PublishResult(
                draft_id=row.id,
                status="failed",
                subreddit=row.subreddit,
                auto_published=True,
                error_message=row.error_message,
            )
        response.status = row.status  # type: ignore[assignment]
    return response


def _flair_id_for(flairs: list[dict[str, str]], text: str | None) -> str | None:
    if not text:
        return None
    wanted = text.strip().lower()
    for flair in flairs:
        if (flair.get("text") or "").strip().lower() == wanted:
            return flair.get("id") or None
    return None


def _draft_response(
    row: RedditPost, draft: RedditDraftSchema, profile: StoreProfileSummary
) -> RedditDraftResponse:
    return RedditDraftResponse(
        draft_id=row.id,
        status=row.status,  # type: ignore[arg-type]
        subreddit=row.subreddit,
        angle=PostAngle(row.angle),
        kind=row.kind,  # type: ignore[arg-type]
        title=row.title,
        body_markdown=row.body_markdown,
        suggested_flair_text=draft.suggested_flair_text,
        flair_id=row.flair_id,
        disclosure_included=draft.disclosure_included,
        fit_score=draft.fit_score,
        rule_check_notes=draft.rule_check_notes,
        gemini_model=row.gemini_model,
        created_at=row.created_at,
        store_profile=profile,
    )


@router.post("/drafts/{draft_id}/publish", response_model=PublishResult)
async def publish_draft(
    request: Request,
    draft_id: str,
    payload: RedditPublishRequest,
    session: AsyncSession = Depends(get_session),
) -> PublishResult:
    """Publish one draft, applying the reviewer's edits."""
    user_id = _require_user(request)
    row = await session.get(RedditPost, draft_id)
    if row is None or row.user_id != user_id:
        raise HTTPException(status_code=404, detail="Draft not found")
    if row.status == "published":
        raise HTTPException(
            status_code=409, detail=f"This draft was already published: {row.permalink}"
        )

    if payload.title is not None:
        row.title = payload.title.strip()
    if payload.body_markdown is not None:
        row.body_markdown = payload.body_markdown.strip()
    if payload.flair_id is not None:
        row.flair_id = payload.flair_id or None
    if payload.kind is not None:
        row.kind = payload.kind

    if not row.title:
        raise HTTPException(status_code=422, detail="The title must not be empty")
    if len(row.title) > TITLE_MAX_CHARS:
        raise HTTPException(
            status_code=422, detail=f"The title exceeds Reddit's {TITLE_MAX_CHARS}-character limit"
        )
    if row.kind == "self" and not row.body_markdown:
        raise HTTPException(status_code=422, detail="A text post needs a body")
    if len(row.body_markdown) > BODY_MAX_CHARS:
        raise HTTPException(
            status_code=422, detail=f"The body exceeds Reddit's {BODY_MAX_CHARS}-character limit"
        )

    result = await _publish(session, user_id, row, auto=False)
    if result.status == "failed":
        raise HTTPException(status_code=502, detail=result.error_message or "Reddit refused the post")
    return result


async def _publish(
    session: AsyncSession, user_id: str, row: RedditPost, *, auto: bool
) -> PublishResult:
    """Guardrails, then one submit. Never retries, never edits Reddit's message."""
    try:
        await check_publish_allowed(session, user_id, row.store_url, row.subreddit)
    except GuardrailError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    client = await _client(user_id)
    row.auto_published = auto
    try:
        submitted = await client.submit_post(
            subreddit=row.subreddit,
            title=row.title,
            kind=row.kind,
            text=row.body_markdown if row.kind == "self" else None,
            url=row.store_url if row.kind == "link" else None,
            flair_id=row.flair_id,
        )
    except RedditRateLimited as exc:
        row.status = "failed"
        row.error_message = exc.message
        await session.commit()
        raise HTTPException(status_code=429, detail=exc.message) from exc
    except RedditError as exc:
        row.status = "failed"
        row.error_message = getattr(exc, "message", str(exc))
        await session.commit()
        logger.warning("Reddit refused draft %s: %s", row.id, row.error_message)
        if auto:
            # In the auto flow the caller reports the failure in the draft response.
            return _publish_result(row, client)
        raise _http_for(exc) from exc

    row.status = "published"
    row.reddit_post_id = submitted.get("id") or None
    row.permalink = submitted.get("url") or None
    row.published_at = datetime.now(UTC)
    row.error_message = None
    await session.commit()
    return _publish_result(row, client)


def _publish_result(row: RedditPost, client: RedditClient) -> PublishResult:
    return PublishResult(
        draft_id=row.id,
        status=row.status,  # type: ignore[arg-type]
        subreddit=row.subreddit,
        permalink=row.permalink,
        reddit_post_id=row.reddit_post_id,
        published_at=row.published_at,
        auto_published=row.auto_published,
        error_message=row.error_message,
        ratelimit_remaining=client.ratelimit.get("remaining"),
    )


# --- History ---------------------------------------------------------------


@router.get("/posts", response_model=RedditPostsResponse)
async def list_posts(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> RedditPostsResponse:
    """Every draft and publish for this session, newest first."""
    user_id = _require_user(request)
    result = await session.execute(
        select(RedditPost)
        .where(RedditPost.user_id == user_id)
        .order_by(desc(RedditPost.created_at))
        .limit(limit)
    )
    return RedditPostsResponse(posts=[_summary(row) for row in result.scalars()])
