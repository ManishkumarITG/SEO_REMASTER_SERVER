"""RedditClient: authorize URL, token exchange, submit, and error mapping.

No network: respx serves every Reddit endpoint. Nothing here touches a real account.
"""

from __future__ import annotations

import base64
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from config import get_settings
from integrations.reddit.client import (
    OAUTH_BASE,
    TOKEN_URL,
    RedditAPIError,
    RedditAuthError,
    RedditClient,
    RedditRateLimited,
    has_self_promo_rule,
)

CLIENT_ID = "test-client-id"
CLIENT_SECRET = "test-client-secret"


@pytest.fixture(autouse=True)
def reddit_config(monkeypatch: pytest.MonkeyPatch):
    """get_settings() is lru_cached, so patch the cached instance itself."""
    settings = get_settings()
    monkeypatch.setattr(settings, "reddit_client_id", CLIENT_ID, raising=False)
    monkeypatch.setattr(settings, "reddit_client_secret", CLIENT_SECRET, raising=False)
    monkeypatch.setattr(
        settings, "reddit_redirect_uri", "http://localhost:8000/auth/reddit/callback", raising=False
    )
    monkeypatch.setattr(settings, "reddit_user_agent", "web:seo-analyzer:test (by /u/tester)", raising=False)
    monkeypatch.setattr(settings, "reddit_scopes", "identity submit read mysubreddits flair", raising=False)
    return settings


def test_authorize_url_carries_everything_reddit_needs() -> None:
    url = RedditClient.build_authorize_url("state-abc")
    query = parse_qs(urlsplit(url).query)

    assert urlsplit(url).netloc == "www.reddit.com"
    assert query["client_id"] == [CLIENT_ID]
    assert query["response_type"] == ["code"]
    assert query["state"] == ["state-abc"]
    assert query["redirect_uri"] == ["http://localhost:8000/auth/reddit/callback"]
    # duration=permanent is what makes Reddit return a refresh token at all.
    assert query["duration"] == ["permanent"]
    assert query["scope"] == ["identity submit read mysubreddits flair"]
    # The secret must never appear in a URL the browser sees.
    assert CLIENT_SECRET not in url


@respx.mock
async def test_exchange_code_uses_basic_auth_and_the_right_form() -> None:
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "access-1",
                "refresh_token": "refresh-1",
                "expires_in": 3600,
                "scope": "identity submit",
            },
        )
    )
    client = RedditClient()
    payload = await client.exchange_code("code-xyz")

    assert payload["access_token"] == "access-1"
    request = route.calls.last.request
    expected = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    assert request.headers["Authorization"] == f"Basic {expected}"
    assert request.headers["User-Agent"] == "web:seo-analyzer:test (by /u/tester)"
    form = parse_qs(request.content.decode())
    assert form["grant_type"] == ["authorization_code"]
    assert form["code"] == ["code-xyz"]
    assert form["redirect_uri"] == ["http://localhost:8000/auth/reddit/callback"]


@respx.mock
async def test_refresh_sends_refresh_grant() -> None:
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "access-2", "expires_in": 3600})
    )
    client = RedditClient(refresh_token="refresh-1")
    await client.refresh()

    form = parse_qs(route.calls.last.request.content.decode())
    assert form["grant_type"] == ["refresh_token"]
    assert form["refresh_token"] == ["refresh-1"]


@respx.mock
async def test_bad_client_credentials_raise_auth_error() -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(401))
    with pytest.raises(RedditAuthError):
        await RedditClient().exchange_code("code-xyz")


@respx.mock
async def test_submit_post_sends_the_expected_form() -> None:
    route = respx.post(f"{OAUTH_BASE}/api/submit").mock(
        return_value=httpx.Response(
            200,
            json={
                "json": {
                    "errors": [],
                    "data": {
                        "id": "abc123",
                        "name": "t3_abc123",
                        "url": "https://www.reddit.com/r/test/comments/abc123/hello/",
                    },
                }
            },
            headers={"X-Ratelimit-Used": "3", "X-Ratelimit-Remaining": "597.0", "X-Ratelimit-Reset": "412"},
        )
    )
    client = RedditClient(access_token="access-1")
    result = await client.submit_post(
        subreddit="test", title="Hello", kind="self", text="Body text", flair_id="flair-1"
    )

    assert result == {
        "id": "abc123",
        "name": "t3_abc123",
        "url": "https://www.reddit.com/r/test/comments/abc123/hello/",
    }
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer access-1"
    assert request.headers["User-Agent"] == "web:seo-analyzer:test (by /u/tester)"
    form = parse_qs(request.content.decode())
    assert form["api_type"] == ["json"]
    assert form["sr"] == ["test"]
    assert form["kind"] == ["self"]
    assert form["title"] == ["Hello"]
    assert form["text"] == ["Body text"]
    assert form["flair_id"] == ["flair-1"]
    # Rate-limit headers are captured for the UI footer.
    assert client.ratelimit == {"used": 3.0, "remaining": 597.0, "reset": 412.0}


@respx.mock
async def test_link_post_sends_url_instead_of_text() -> None:
    route = respx.post(f"{OAUTH_BASE}/api/submit").mock(
        return_value=httpx.Response(200, json={"json": {"errors": [], "data": {"id": "x"}}})
    )
    client = RedditClient(access_token="access-1")
    await client.submit_post(
        subreddit="test", title="Hello", kind="link", url="https://shop.example.com/"
    )

    form = parse_qs(route.calls.last.request.content.decode())
    assert form["url"] == ["https://shop.example.com/"]
    assert "text" not in form


@respx.mock
async def test_ratelimit_error_in_body_keeps_reddits_message() -> None:
    message = "you are doing that too much. try again in 9 minutes."
    respx.post(f"{OAUTH_BASE}/api/submit").mock(
        return_value=httpx.Response(
            200, json={"json": {"errors": [["RATELIMIT", message, "ratelimit"]], "data": {}}}
        )
    )
    client = RedditClient(access_token="access-1")
    with pytest.raises(RedditRateLimited) as excinfo:
        await client.submit_post(subreddit="test", title="Hello", kind="self", text="Body")

    # Reddit's wording reaches the UI verbatim.
    assert excinfo.value.message == message


@respx.mock
async def test_other_submit_error_becomes_api_error() -> None:
    respx.post(f"{OAUTH_BASE}/api/submit").mock(
        return_value=httpx.Response(
            200,
            json={
                "json": {
                    "errors": [["SUBREDDIT_NOTALLOWED", "you aren't allowed to post there.", "sr"]],
                    "data": {},
                }
            },
        )
    )
    client = RedditClient(access_token="access-1")
    with pytest.raises(RedditAPIError) as excinfo:
        await client.submit_post(subreddit="test", title="Hello", kind="self", text="Body")

    assert excinfo.value.code == "SUBREDDIT_NOTALLOWED"
    assert excinfo.value.message == "you aren't allowed to post there."


@respx.mock
async def test_http_429_becomes_rate_limited_and_is_not_retried() -> None:
    route = respx.post(f"{OAUTH_BASE}/api/submit").mock(
        return_value=httpx.Response(429, headers={"X-Ratelimit-Reset": "60"})
    )
    client = RedditClient(access_token="access-1")
    with pytest.raises(RedditRateLimited) as excinfo:
        await client.submit_post(subreddit="test", title="Hello", kind="self", text="Body")

    assert excinfo.value.reset_seconds == 60.0
    assert route.call_count == 1  # never auto-retried


@respx.mock
async def test_401_refreshes_once_then_retries() -> None:
    submit = respx.post(f"{OAUTH_BASE}/api/submit")
    submit.side_effect = [
        httpx.Response(401),
        httpx.Response(200, json={"json": {"errors": [], "data": {"id": "ok"}}}),
    ]
    token = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "access-2", "expires_in": 3600})
    )
    client = RedditClient(access_token="stale", refresh_token="refresh-1")
    result = await client.submit_post(subreddit="test", title="Hello", kind="self", text="Body")

    assert result["id"] == "ok"
    assert token.call_count == 1
    assert submit.call_count == 2
    assert submit.calls[1].request.headers["Authorization"] == "Bearer access-2"


@respx.mock
async def test_get_me_maps_identity_fields() -> None:
    respx.get(f"{OAUTH_BASE}/api/v1/me").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": "tester",
                "link_karma": 12,
                "comment_karma": 340,
                "created_utc": 1_600_000_000,
            },
        )
    )
    identity = await RedditClient(access_token="access-1").get_me()
    assert identity == {
        "name": "tester",
        "link_karma": 12,
        "comment_karma": 340,
        "created_utc": 1_600_000_000,
    }


@respx.mock
async def test_my_subreddits_puts_moderated_first_and_deduplicates() -> None:
    def listing(*names: tuple[str, int]) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "children": [
                        {"data": {"display_name": name, "title": name, "subscribers": subs}}
                        for name, subs in names
                    ]
                }
            },
        )

    respx.get(f"{OAUTH_BASE}/subreddits/mine/moderator").mock(
        return_value=listing(("myshop", 120))
    )
    respx.get(f"{OAUTH_BASE}/subreddits/mine/subscriber").mock(
        return_value=listing(("myshop", 120), ("candles", 90_000), ("crafts", 5_000))
    )

    moderated, subscribed = await RedditClient(access_token="access-1").get_my_subreddits()

    assert [sub["name"] for sub in moderated] == ["myshop"]
    assert moderated[0]["you_moderate"] is True
    # A moderated sub is not repeated in the subscribed list, and bigger subs sort first.
    assert [sub["name"] for sub in subscribed] == ["candles", "crafts"]


@respx.mock
async def test_link_flairs_403_returns_empty_list() -> None:
    respx.get(f"{OAUTH_BASE}/r/test/api/link_flair_v2").mock(return_value=httpx.Response(403))
    assert await RedditClient(access_token="access-1").get_link_flairs("test") == []


@respx.mock
async def test_subreddit_rules_flag_self_promo(monkeypatch: pytest.MonkeyPatch) -> None:
    import utils.cache as cache_module

    # Fresh cache so this test never reads another test's rules.
    monkeypatch.setattr(cache_module, "_cache", None)
    respx.get(f"{OAUTH_BASE}/r/test/about/rules").mock(
        return_value=httpx.Response(
            200,
            json={
                "rules": [
                    {"short_name": "No self-promotion", "description": "Do not advertise your shop."}
                ],
                "site_rules": ["No spam"],
            },
        )
    )
    respx.get(f"{OAUTH_BASE}/r/test/about").mock(
        return_value=httpx.Response(
            200, json={"data": {"public_description": "A test sub", "title": "Test"}}
        )
    )

    rules = await RedditClient(access_token="access-1").get_subreddit_rules("test")

    assert rules["self_promo_warning"] is True
    assert rules["public_description"] == "A test sub"
    assert rules["rules"][0]["short_name"] == "No self-promotion"


def test_has_self_promo_rule_is_a_pure_check() -> None:
    assert has_self_promo_rule(["No self-promotion of any kind"]) is True
    assert has_self_promo_rule(["Posts must be advertising-free"]) is True
    assert has_self_promo_rule(["Be kind", "Use flair"]) is False
    assert has_self_promo_rule([]) is False
