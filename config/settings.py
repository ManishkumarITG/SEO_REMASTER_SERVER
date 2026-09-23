"""Application settings, loaded from environment / .env via pydantic-settings."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolved from __file__, never the cwd: production process managers (systemd,
# Docker, pm2) rarely start the app with cwd = server/, and a cwd-relative
# .env silently loads nothing there - every key falls back to its default, so the
# app boots "successfully" with no credentials and an ephemeral session secret.
#
# NOTE the `.parent.parent`: this module is `server/config/settings.py`, so one
# .parent is `server/config` and two is `server/` - the directory that actually
# holds .env and seo.db. Getting this wrong does not raise; it silently loads no
# .env at all and points DATABASE_URL at a fresh empty database.
SERVER_DIR = Path(__file__).resolve().parent.parent


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

    # Store catalog crawler. Discovery is never capped; enrichment (one <head>
    # fetch per item, ~0.2 s each) is, so a 10,000-product store finishes
    # "partial" instead of holding a crawl slot for half an hour.
    crawl_max_enrich_items: int = 2000
    crawl_enrich_concurrency: int = 8
    # Honest bot identity with a contact URL. Never a spoofed browser UA: several
    # stores throttle named SEO bots specifically, and cloaking is a ToS breach.
    crawler_user_agent: str = "SEOAnalyzerBot/0.1 (+https://github.com/seo-remaster/bot)"
    # HTTP/2 lets an aborted head-only read keep its connection alive, which should
    # matter for large catalogs. Measured on a 23-item crawl it made no difference
    # (6.88 s vs 6.95 s), so it is opt-in: needs `pip install "httpx[http2]"` and
    # silently falls back to HTTP/1.1 if h2 is absent.
    crawl_http2: bool = False

    # Site-wide crawl (services/crawler/site_crawl.py). Separate knobs from the
    # store catalog crawl above: this one fetches whole pages and discovers its
    # own frontier, so it is bounded by pages AND depth AND wall clock, and it
    # paces itself more slowly than a <head>-only pass.
    site_crawl_max_pages: int = 500
    site_crawl_max_depth: int = 5
    site_crawl_concurrency: int = 6
    # 30 minutes, not 10. A measured 298-page crawl of a real store took 537s, so
    # 600s was about to become the binding limit on any site slightly larger -
    # and now that a 429 is answered by waiting rather than giving up, a
    # rate-limited crawl legitimately needs longer. The Stop button is the
    # interruption that matters; this is only the backstop.
    site_crawl_deadline_s: int = 30 * 60
    #: Floor between request starts. robots Crawl-delay raises it, never lowers it.
    site_crawl_min_interval_s: float = 0.1
    #: Off by default: a crawl seeded at example.com that wanders into
    #: shop.example.com is usually a surprise, not a feature.
    site_crawl_include_subdomains: bool = False
    # Analyzers run once per crawled page. Only the two that read the DOM they
    # are handed belong here: pagespeed, content_ai and search_console each make
    # their own outbound call, so at 500 pages they would mean 500 Lighthouse
    # runs or 500 LLM completions. services/site_crawl_analysis.py refuses any
    # name outside its bulk-safe set. Empty string turns page scoring off.
    site_crawl_analyzers: str = "on_page,technical"

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


#: Markers that mean "someone copied a template and never filled it in". Matched
#: case-insensitively against every string-valued setting. Only key NAMES are ever
#: logged - never the values, which are real secrets on a healthy install. The
#: mongo marker is the full placeholder host rather than bare "xxxxx" so a real
#: key that happens to contain five x's is never mistaken for a template.
PLACEHOLDER_MARKERS: tuple[str, ...] = ("REPLACE_ME", "yourdomain.com", "USER:PASSWORD", "xxxxx.mongodb.net")


def placeholder_env_values() -> list[str]:
    """Settings whose value is still an unfilled template placeholder, by UPPER_CASE key.

    `unknown_env_keys` catches a misspelled KEY. This catches a recognised key whose
    VALUE was never filled in: `GOOGLE_REDIRECT_URI=https://api.yourdomain.com/...`
    boots "successfully" and then fails at the first OAuth redirect with a Google
    error page that says nothing about .env. Naming the key at startup turns a
    twenty-minute hunt into a log line.
    """
    settings = get_settings()
    flagged: list[str] = []
    for name in Settings.model_fields:
        value = getattr(settings, name, None)
        if not isinstance(value, str) or not value:
            continue
        lowered = value.lower()
        if any(marker.lower() in lowered for marker in PLACEHOLDER_MARKERS):
            flagged.append(name.upper())
        elif name == "reddit_user_agent" and "change_me" in lowered and settings.reddit_configured:
            # The shipped default UA says CHANGE_ME on purpose. It is only a problem
            # once Reddit is actually configured - and then Reddit throttles it.
            flagged.append(name.upper())
    return flagged
