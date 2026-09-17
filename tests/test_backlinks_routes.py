"""Backlink routes: config gating, connection gating, guardrails, and the publish path.

No network: the Reddit client and Gemini are both stubbed, so nothing reaches
reddit.com or Google. Each test uses a fresh random user id, so rows never leak
between tests in the shared dev database.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import api.routes.backlinks as backlinks
import main
from config import get_settings
from db.session import async_session_factory, init_db
from models.db import RedditCredential, RedditPost, User
from services.backlinks.reddit_post_generator import RedditDraftSchema
from services.integrations.reddit import (
    RedditNotConfigured,
    RedditNotConnected,
    RedditRateLimited,
)

DRAFT_PAYLOAD = {
    "store_url": "https://shop.example.com/",
    "subreddit": "candlemaking",
    "angle": "intro",
    "kind": "self",
    "auto_publish": False,
}

GENERATED = RedditDraftSchema(
    title="We have poured candles by hand since 2019",
    body_markdown="We run a small candle shop. Here is what we learned.",
    suggested_flair_text=None,
    disclosure_included=True,
    fit_score=4,
    rule_check_notes=["Rule 1: the shop is mentioned once, at the end."],
)


class StubRedditClient:
    """Enough of RedditClient for the routes, with no network underneath."""

    def __init__(self, *, submit: dict[str, str] | None = None, error: Exception | None = None):
        self.ratelimit: dict[str, float | None] = {"used": 2.0, "remaining": 598.0, "reset": 300.0}
        self._submit = submit or {
            "id": "abc123",
            "name": "t3_abc123",
            "url": "https://www.reddit.com/r/candlemaking/comments/abc123/x/",
        }
        self._error = error
        self.submissions: list[dict[str, Any]] = []

    async def get_subreddit_rules(self, name: str) -> dict[str, Any]:
        return {
            "rules": [{"short_name": "Be kind", "description": "No abuse."}],
            "site_rules": [],
            "public_description": "For people who make candles.",
            "title": "Candle Making",
            "submission_type": "any",
            "self_promo_warning": False,
        }

    async def get_link_flairs(self, name: str) -> list[dict[str, str]]:
        return [{"id": "flair-1", "text": "Discussion", "text_editable": False}]

    async def get_my_subreddits(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return (
            [{"name": "myshop", "title": "My Shop", "subscribers": 120, "you_moderate": True}],
            [{"name": "candlemaking", "title": "Candle Making", "subscribers": 90_000, "you_moderate": False}],
        )

    async def submit_post(self, **kwargs: Any) -> dict[str, str]:
        self.submissions.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._submit


class StubGemini:
    model = "stub-model"
    is_configured = True

    def __init__(self, draft: RedditDraftSchema = GENERATED) -> None:
        self._draft = draft

    async def generate_structured(self, **kwargs: Any) -> RedditDraftSchema:
        return self._draft


@pytest.fixture(autouse=True)
def configured(monkeypatch: pytest.MonkeyPatch):
    """Keys present by default; get_settings() is lru_cached so patch the instance."""
    settings = get_settings()
    monkeypatch.setattr(settings, "reddit_client_id", "cid", raising=False)
    monkeypatch.setattr(settings, "reddit_client_secret", "secret", raising=False)
    monkeypatch.setattr(settings, "gemini_api_key", "gkey", raising=False)
    monkeypatch.setattr(settings, "reddit_max_posts_per_day", 5, raising=False)
    monkeypatch.setattr(settings, "reddit_min_minutes_between_posts", 10, raising=False)
    monkeypatch.setattr(settings, "reddit_dedupe_days", 30, raising=False)
    return settings


@pytest.fixture
def user_id(monkeypatch: pytest.MonkeyPatch) -> str:
    """A signed-in session, without going through the OAuth round-trip."""
    uid = str(uuid4())
    monkeypatch.setattr(backlinks, "_require_user", lambda request: uid)
    return uid


def _client() -> TestClient:
    return TestClient(main.app)


async def _seed_user(user_id: str, *, with_credential: bool = True) -> None:
    await init_db()
    async with async_session_factory() as session:
        session.add(User(id=user_id))
        if with_credential:
            session.add(
                RedditCredential(
                    user_id=user_id,
                    reddit_username="tester",
                    access_token="access-1",
                    refresh_token="refresh-1",
                    token_expires_at=datetime.now(UTC) + timedelta(hours=1),
                    scopes="identity submit",
                )
            )
        await session.commit()


async def _seed_post(user_id: str, **overrides: Any) -> str:
    await init_db()
    fields: dict[str, Any] = {
        "user_id": user_id,
        "store_url": "https://shop.example.com/",
        "store_name": "Ember & Oak",
        "subreddit": "candlemaking",
        "angle": "intro",
        "kind": "self",
        "title": "A draft title",
        "body_markdown": "A draft body long enough to publish.",
        "status": "draft",
    }
    fields.update(overrides)
    row = RedditPost(**fields)
    async with async_session_factory() as session:
        session.add(row)
        await session.commit()
        return row.id


# --- Gating ---------------------------------------------------------------


def test_endpoints_401_without_a_session() -> None:
    with _client() as client:
        assert client.get("/api/backlinks/reddit/subreddits").status_code == 401
        assert client.get("/api/backlinks/reddit/posts").status_code == 401
        assert client.post("/api/backlinks/reddit/drafts", json=DRAFT_PAYLOAD).status_code == 401


def test_reddit_login_503_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "reddit_client_id", "", raising=False)
    monkeypatch.setattr(settings, "reddit_client_secret", "", raising=False)
    with _client() as client:
        response = client.get("/auth/reddit/login", follow_redirects=False)
        assert response.status_code == 503
        assert "REDDIT_CLIENT_ID" in response.json()["detail"]


def test_subreddits_503_when_reddit_unconfigured(
    monkeypatch: pytest.MonkeyPatch, user_id: str
) -> None:
    async def unconfigured(_user_id: str) -> Any:
        raise RedditNotConfigured("Reddit is not configured - set REDDIT_CLIENT_ID")

    monkeypatch.setattr(backlinks, "get_client_for_user", unconfigured)
    with _client() as client:
        response = client.get("/api/backlinks/reddit/subreddits")
        assert response.status_code == 503
        assert "REDDIT_CLIENT_ID" in response.json()["detail"]


def test_subreddits_401_when_not_connected(monkeypatch: pytest.MonkeyPatch, user_id: str) -> None:
    async def not_connected(_user_id: str) -> Any:
        raise RedditNotConnected("Reddit is not connected for this session")

    monkeypatch.setattr(backlinks, "get_client_for_user", not_connected)
    with _client() as client:
        assert client.get("/api/backlinks/reddit/subreddits").status_code == 401


def test_drafts_503_when_gemini_unconfigured(
    monkeypatch: pytest.MonkeyPatch, user_id: str
) -> None:
    class Unconfigured:
        is_configured = False
        model = ""

    monkeypatch.setattr(backlinks, "get_gemini_service", lambda: Unconfigured())
    with _client() as client:
        response = client.post("/api/backlinks/reddit/drafts", json=DRAFT_PAYLOAD)
        assert response.status_code == 503
        assert "GEMINI_API_KEY" in response.json()["detail"]


def test_subreddits_lists_moderated_first(monkeypatch: pytest.MonkeyPatch, user_id: str) -> None:
    stub = StubRedditClient()
    monkeypatch.setattr(backlinks, "get_client_for_user", _returning(stub))
    with _client() as client:
        payload = client.get("/api/backlinks/reddit/subreddits").json()
    assert [sub["name"] for sub in payload["moderated"]] == ["myshop"]
    assert payload["moderated"][0]["you_moderate"] is True
    assert [sub["name"] for sub in payload["subscribed"]] == ["candlemaking"]


def _returning(stub: StubRedditClient):
    async def _get_client(_user_id: str) -> StubRedditClient:
        return stub

    return _get_client


# --- Draft generation -----------------------------------------------------


async def test_draft_is_generated_and_stored_without_publishing(
    monkeypatch: pytest.MonkeyPatch, user_id: str
) -> None:
    await _seed_user(user_id)
    stub = StubRedditClient()
    monkeypatch.setattr(backlinks, "get_client_for_user", _returning(stub))
    monkeypatch.setattr(backlinks, "get_gemini_service", lambda: StubGemini())
    monkeypatch.setattr(backlinks, "build_store_profile", _profile_stub())

    with _client() as client:
        response = client.post("/api/backlinks/reddit/drafts", json=DRAFT_PAYLOAD)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "draft"
    assert body["title"] == GENERATED.title
    assert body["fit_score"] == 4
    assert body["disclosure_included"] is True
    assert body["store_profile"]["store_name"] == "Ember & Oak"
    assert body["publish"] is None
    # Review-before-publish: generating must not have submitted anything.
    assert stub.submissions == []


def _profile_stub():
    from services.backlinks.store_profile import StoreProfile

    async def _build(store_url: str, **kwargs: Any) -> StoreProfile:
        return StoreProfile(
            store_url=store_url,
            store_name="Ember & Oak",
            tagline="Hand-poured candles",
            description_excerpt="We started in a kitchen in 2019.",
            is_shopify=True,
            categories=["Candle"],
            products_source="products_json",
        )

    return _build


async def test_auto_publish_publishes_in_the_same_response(
    monkeypatch: pytest.MonkeyPatch, user_id: str
) -> None:
    await _seed_user(user_id)
    stub = StubRedditClient()
    monkeypatch.setattr(backlinks, "get_client_for_user", _returning(stub))
    monkeypatch.setattr(backlinks, "get_gemini_service", lambda: StubGemini())
    monkeypatch.setattr(backlinks, "build_store_profile", _profile_stub())

    with _client() as client:
        response = client.post(
            "/api/backlinks/reddit/drafts", json={**DRAFT_PAYLOAD, "auto_publish": True}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["publish"]["status"] == "published"
    assert body["publish"]["permalink"].endswith("/abc123/x/")
    assert body["publish"]["auto_published"] is True
    assert len(stub.submissions) == 1
    assert stub.submissions[0]["subreddit"] == "candlemaking"


# --- Publish + guardrails -------------------------------------------------


async def test_publish_success_returns_permalink_and_ratelimit(
    monkeypatch: pytest.MonkeyPatch, user_id: str
) -> None:
    await _seed_user(user_id)
    draft_id = await _seed_post(user_id)
    stub = StubRedditClient()
    monkeypatch.setattr(backlinks, "get_client_for_user", _returning(stub))

    with _client() as client:
        response = client.post(
            f"/api/backlinks/reddit/drafts/{draft_id}/publish",
            json={"title": "An edited title", "body_markdown": "Edited body."},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "published"
    assert body["permalink"].endswith("/abc123/x/")
    assert body["ratelimit_remaining"] == 598.0
    # The reviewer's edits are what got submitted.
    assert stub.submissions[0]["title"] == "An edited title"
    assert stub.submissions[0]["text"] == "Edited body."


async def test_daily_cap_returns_429(monkeypatch: pytest.MonkeyPatch, user_id: str) -> None:
    await _seed_user(user_id)
    now = datetime.now(UTC)
    for index in range(5):
        await _seed_post(
            user_id,
            status="published",
            subreddit=f"other{index}",
            store_url=f"https://other{index}.example.com/",
            published_at=now - timedelta(hours=index + 1),
        )
    draft_id = await _seed_post(user_id)
    stub = StubRedditClient()
    monkeypatch.setattr(backlinks, "get_client_for_user", _returning(stub))

    with _client() as client:
        response = client.post(f"/api/backlinks/reddit/drafts/{draft_id}/publish", json={})

    assert response.status_code == 429
    assert "Daily limit reached" in response.json()["detail"]
    assert stub.submissions == []  # blocked before Reddit was called


async def test_minimum_gap_returns_429(monkeypatch: pytest.MonkeyPatch, user_id: str) -> None:
    await _seed_user(user_id)
    await _seed_post(
        user_id,
        status="published",
        subreddit="somewhereelse",
        store_url="https://other.example.com/",
        published_at=datetime.now(UTC) - timedelta(minutes=2),
    )
    draft_id = await _seed_post(user_id)
    stub = StubRedditClient()
    monkeypatch.setattr(backlinks, "get_client_for_user", _returning(stub))

    with _client() as client:
        response = client.post(f"/api/backlinks/reddit/drafts/{draft_id}/publish", json={})

    assert response.status_code == 429
    assert "Too soon" in response.json()["detail"]
    assert stub.submissions == []


async def test_dedupe_same_store_and_subreddit_returns_409(
    monkeypatch: pytest.MonkeyPatch, user_id: str
) -> None:
    await _seed_user(user_id)
    await _seed_post(
        user_id,
        status="published",
        permalink="https://www.reddit.com/r/candlemaking/comments/old/x/",
        # Old enough to clear the minimum-gap check, inside the 30-day dedupe window.
        published_at=datetime.now(UTC) - timedelta(days=3),
    )
    draft_id = await _seed_post(user_id)
    stub = StubRedditClient()
    monkeypatch.setattr(backlinks, "get_client_for_user", _returning(stub))

    with _client() as client:
        response = client.post(f"/api/backlinks/reddit/drafts/{draft_id}/publish", json={})

    assert response.status_code == 409
    assert "already posted to r/candlemaking" in response.json()["detail"]
    assert stub.submissions == []


async def test_reddit_ratelimit_is_surfaced_verbatim_and_marks_the_row_failed(
    monkeypatch: pytest.MonkeyPatch, user_id: str
) -> None:
    from services.integrations.reddit import RedditRateLimited

    await _seed_user(user_id)
    draft_id = await _seed_post(user_id)
    message = "you are doing that too much. try again in 9 minutes."
    stub = StubRedditClient(error=RedditRateLimited(message, 540.0))
    monkeypatch.setattr(backlinks, "get_client_for_user", _returning(stub))

    with _client() as client:
        response = client.post(f"/api/backlinks/reddit/drafts/{draft_id}/publish", json={})
        assert response.status_code == 429
        assert response.json()["detail"] == message  # Reddit's own words, unmodified
        assert len(stub.submissions) == 1  # tried exactly once, never retried

        history = client.get("/api/backlinks/reddit/posts").json()["posts"]

    failed = next(post for post in history if post["draft_id"] == draft_id)
    assert failed["status"] == "failed"
    assert failed["error_message"] == message


async def test_reddit_refusal_becomes_502_with_reddits_message(
    monkeypatch: pytest.MonkeyPatch, user_id: str
) -> None:
    from services.integrations.reddit import RedditAPIError

    await _seed_user(user_id)
    draft_id = await _seed_post(user_id)
    stub = StubRedditClient(
        error=RedditAPIError("SUBREDDIT_NOTALLOWED", "you aren't allowed to post there.")
    )
    monkeypatch.setattr(backlinks, "get_client_for_user", _returning(stub))

    with _client() as client:
        response = client.post(f"/api/backlinks/reddit/drafts/{draft_id}/publish", json={})

    assert response.status_code == 502
    assert response.json()["detail"] == "you aren't allowed to post there."


async def test_publishing_twice_is_refused(monkeypatch: pytest.MonkeyPatch, user_id: str) -> None:
    await _seed_user(user_id)
    draft_id = await _seed_post(
        user_id,
        status="published",
        permalink="https://www.reddit.com/r/candlemaking/comments/done/x/",
        published_at=datetime.now(UTC),
    )
    monkeypatch.setattr(backlinks, "get_client_for_user", _returning(StubRedditClient()))

    with _client() as client:
        response = client.post(f"/api/backlinks/reddit/drafts/{draft_id}/publish", json={})

    assert response.status_code == 409
    assert "already published" in response.json()["detail"]


async def test_over_long_title_is_rejected_before_reddit(
    monkeypatch: pytest.MonkeyPatch, user_id: str
) -> None:
    await _seed_user(user_id)
    draft_id = await _seed_post(user_id)
    stub = StubRedditClient()
    monkeypatch.setattr(backlinks, "get_client_for_user", _returning(stub))

    with _client() as client:
        response = client.post(
            f"/api/backlinks/reddit/drafts/{draft_id}/publish", json={"title": "x" * 301}
        )

    assert response.status_code == 422
    assert stub.submissions == []


async def test_another_users_draft_is_not_publishable(
    monkeypatch: pytest.MonkeyPatch, user_id: str
) -> None:
    await _seed_user(user_id)
    other_draft = await _seed_post(str(uuid4()))
    monkeypatch.setattr(backlinks, "get_client_for_user", _returning(StubRedditClient()))

    with _client() as client:
        response = client.post(f"/api/backlinks/reddit/drafts/{other_draft}/publish", json={})

    assert response.status_code == 404


# --- Health ---------------------------------------------------------------


def test_health_reports_reddit_and_gemini_configuration() -> None:
    with _client() as client:
        payload = client.get("/api/health").json()
    assert payload["reddit"] == "configured"
    assert payload["gemini"] == "configured"
    # Keys themselves must never appear in the response.
    assert "cid" not in str(payload)
    assert "gkey" not in str(payload)


async def test_auto_publish_blocked_by_a_guardrail_still_returns_the_draft(
    monkeypatch: pytest.MonkeyPatch, user_id: str
) -> None:
    """A blocked auto-publish must not strand the draft it already paid Gemini for."""
    await _seed_user(user_id)
    await _seed_post(
        user_id,
        status="published",
        # Two days back: outside the daily cap and the minimum gap, but well
        # inside the 30-day dedupe window, so dedupe is the rule under test.
        published_at=datetime.now(UTC) - timedelta(days=2),
        permalink="https://reddit.com/r/candlemaking/comments/earlier/",
    )
    stub = StubRedditClient()
    monkeypatch.setattr(backlinks, "get_client_for_user", _returning(stub))
    monkeypatch.setattr(backlinks, "get_gemini_service", lambda: StubGemini())
    monkeypatch.setattr(backlinks, "build_store_profile", _profile_stub())

    with _client() as client:
        response = client.post(
            "/api/backlinks/reddit/drafts", json={**DRAFT_PAYLOAD, "auto_publish": True}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["draft_id"]  # the draft is recoverable, not lost to a bare 429
    assert body["publish"]["status"] == "failed"
    assert "already posted" in body["publish"]["error_message"]
    assert stub.submissions == []  # the guardrail ran before any Reddit call


async def test_auto_publish_rate_limited_still_returns_the_draft(
    monkeypatch: pytest.MonkeyPatch, user_id: str
) -> None:
    await _seed_user(user_id)
    stub = StubRedditClient(error=RedditRateLimited("you are doing that too much. try again in 8 minutes.", 480.0))
    monkeypatch.setattr(backlinks, "get_client_for_user", _returning(stub))
    monkeypatch.setattr(backlinks, "get_gemini_service", lambda: StubGemini())
    monkeypatch.setattr(backlinks, "build_store_profile", _profile_stub())

    with _client() as client:
        response = client.post(
            "/api/backlinks/reddit/drafts", json={**DRAFT_PAYLOAD, "auto_publish": True}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["publish"]["status"] == "failed"
    # Reddit's own wording reaches the UI unchanged.
    assert "try again in 8 minutes" in body["publish"]["error_message"]
