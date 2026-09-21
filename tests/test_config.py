"""Startup config checks: placeholder values are named by key, and never printed.

`get_settings()` is `@lru_cache`d, so every test patches the cached instance (the
project's established pattern) rather than setting env vars after import.
"""

from __future__ import annotations

import pytest

from config import PLACEHOLDER_MARKERS, get_settings, placeholder_env_values


def _clean_slate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whatever the developer's real .env holds, start from a config that reports nothing."""
    settings = get_settings()
    for key in placeholder_env_values():
        monkeypatch.setattr(settings, key.lower(), "filled-in", raising=False)
    monkeypatch.setattr(settings, "reddit_client_id", "", raising=False)
    monkeypatch.setattr(settings, "reddit_client_secret", "", raising=False)
    assert placeholder_env_values() == []


def test_a_template_redirect_uri_is_reported_by_key_name_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact misconfiguration behind Google's redirect_uri_mismatch page."""
    _clean_slate(monkeypatch)
    monkeypatch.setattr(get_settings(), "google_redirect_uri", "https://api.yourdomain.com/auth/callback")

    flagged = placeholder_env_values()

    assert flagged == ["GOOGLE_REDIRECT_URI"]
    assert not any("yourdomain" in entry for entry in flagged), "values must never leak into the report"


def test_a_replace_me_secret_is_reported_without_revealing_it(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_slate(monkeypatch)
    monkeypatch.setattr(get_settings(), "google_client_secret", "REPLACE_ME")

    flagged = placeholder_env_values()

    assert flagged == ["GOOGLE_CLIENT_SECRET"]
    assert "REPLACE_ME" not in " ".join(flagged)


def test_the_atlas_template_mongo_url_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_slate(monkeypatch)
    monkeypatch.setattr(
        get_settings(), "mongodb_url", "mongodb+srv://USER:PASSWORD@cluster0.xxxxx.mongodb.net/?retryWrites=true"
    )
    assert placeholder_env_values() == ["MONGODB_URL"]


def test_a_real_key_containing_five_xs_is_not_mistaken_for_a_template(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_slate(monkeypatch)
    monkeypatch.setattr(get_settings(), "anthropic_api_key", "sk-ant-abcxxxxxdef0123456789")
    assert placeholder_env_values() == []


def test_matching_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_slate(monkeypatch)
    monkeypatch.setattr(get_settings(), "frontend_url", "https://APP.YourDomain.COM")
    assert placeholder_env_values() == ["FRONTEND_URL"]


def test_change_me_user_agent_only_matters_once_reddit_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """CHANGE_ME is the shipped default; warning on every boot for non-Reddit users would be noise."""
    _clean_slate(monkeypatch)
    settings = get_settings()
    monkeypatch.setattr(settings, "reddit_user_agent", "web:seo-analyzer:v0.1.0 (by /u/CHANGE_ME)")

    assert placeholder_env_values() == []  # Reddit not configured -> silent

    monkeypatch.setattr(settings, "reddit_client_id", "cid")
    monkeypatch.setattr(settings, "reddit_client_secret", "secret")
    assert placeholder_env_values() == ["REDDIT_USER_AGENT"]


def test_every_marker_is_matched() -> None:
    for marker in PLACEHOLDER_MARKERS:
        assert marker.lower() in f"prefix-{marker}-suffix".lower()
