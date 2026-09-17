"""Application settings, loaded from environment / .env via pydantic-settings."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolved from __file__, never the cwd: production process managers (systemd,
# Docker, pm2) rarely start the app with cwd = app/server, and a cwd-relative
# .env silently loads nothing there - every key falls back to its default, so the
# app boots "successfully" with no credentials and an ephemeral session secret.
SERVER_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=SERVER_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Bind address for `python main.py` (see main.py). Keep 127.0.0.1 behind a
    # reverse proxy; set HOST=0.0.0.0 to accept traffic directly.
    host: str = "127.0.0.1"
    port: int = 8000

    anthropic_api_key: str = ""
    llm_model: str = "claude-sonnet-5"
    pagespeed_api_key: str = ""
    # Absolute for the same reason as SERVER_DIR: a cwd-relative sqlite path creates
    # a second, empty database whenever the cwd is not app/server.
    database_url: str = f"sqlite+aiosqlite:///{(SERVER_DIR / 'seo.db').as_posix()}"
    redis_url: str = ""
    cors_origins: str = "http://localhost:5173"
    # Dev convenience: also allow any localhost / private-LAN origin (e.g. the app
    # opened via a 192.168.x.x address). Set to an empty string to disable and
    # allow only the explicit CORS_ORIGINS list.
    cors_origin_regex: str = (
        r"http://(localhost|127\.0\.0\.1|192\.168\.\d{1,3}\.\d{1,3}"
        r"|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(:\d+)?"
    )
    cache_ttl_seconds: int = 3600

    # MongoDB: stores Google Search Console reporting data (never OAuth tokens).
    mongodb_url: str = "mongodb://localhost:27017"
    mongodb_db: str = "seo_remaster"
    mongodb_timeout_ms: int = 1500  # fail fast when MongoDB is unreachable

    # Google Search Console OAuth - all values come from .env, never hardcoded.
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/auth/callback"
    session_secret: str = ""
    frontend_url: str = "http://localhost:5173"

    # Gemini (Reddit draft generation). Empty key -> the drafts endpoint answers 503.
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash"

    # Reddit OAuth (Backlink tab). Empty client id/secret -> /auth/reddit/* answers 503.
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_redirect_uri: str = "http://localhost:8000/auth/reddit/callback"
    # Reddit requires <platform>:<app id>:<version> (by /u/<username>); a generic UA gets throttled.
    reddit_user_agent: str = "web:seo-analyzer:v0.1.0 (by /u/CHANGE_ME)"
    reddit_scopes: str = "identity submit read mysubreddits flair"
    # Volume guardrails: these keep the connected account alive and inside Reddit's terms.
    reddit_max_posts_per_day: int = 5
    reddit_min_minutes_between_posts: int = 10
    reddit_dedupe_days: int = 30

    @property
    def cors_origin_list(self) -> list[str]:
        origins = {origin.strip() for origin in self.cors_origins.split(",") if origin.strip()}
        origins.add(self.frontend_url.rstrip("/"))
        return sorted(origins)

    @property
    def gemini_configured(self) -> bool:
        return bool(self.gemini_api_key)

    @property
    def reddit_configured(self) -> bool:
        return bool(self.reddit_client_id and self.reddit_client_secret)

    @property
    def reddit_scope_list(self) -> list[str]:
        return [scope for scope in self.reddit_scopes.split() if scope]


@lru_cache
def get_settings() -> Settings:
    return Settings()


def unknown_env_keys() -> list[str]:
    """Keys in .env that match no Settings field, and are therefore inert.

    `extra="ignore"` means a typo (GIMINI_API_KEY, REDDIT_CLIENT_SECERT) costs
    nothing at startup and everything at runtime: the feature just stays off with
    no message. Reading the file back and naming the misses turns that into a
    warning. Values are never read here - only key names.
    """
    env_file = SERVER_DIR / ".env"
    if not env_file.exists():
        return []
    fields = {name.upper() for name in Settings.model_fields}
    unknown: list[str] = []
    for raw_line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key and key.upper() not in fields and key not in unknown:
            unknown.append(key)
    return unknown
