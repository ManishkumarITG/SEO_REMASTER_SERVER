"""HTTP routers.

Registration order is preserved exactly as main.py had it: health, audits,
google_auth, search_console, reddit_auth, backlinks - then stores, appended
last, then site_crawl. Order matters for path resolution, so keep it. Both
appends are safe: no existing router has a first-segment path parameter, so
nothing can shadow `/api/stores/*` or `/api/site-crawls/*`, and neither can
change resolution for any existing path.
"""

from __future__ import annotations

from fastapi import FastAPI

from routes import (
    audits,
    backlinks,
    google_auth,
    health,
    reddit_auth,
    search_console,
    site_analysis,
    site_crawl,
    stores,
)


def register_routes(app: FastAPI) -> None:
    app.include_router(health.router)
    app.include_router(audits.router)
    app.include_router(google_auth.router)
    app.include_router(search_console.router)
    app.include_router(reddit_auth.router)
    app.include_router(backlinks.router)
    app.include_router(stores.router)
    app.include_router(site_crawl.router)
    app.include_router(site_analysis.router)


__all__ = [
    "audits",
    "backlinks",
    "google_auth",
    "health",
    "reddit_auth",
    "register_routes",
    "search_console",
    "site_analysis",
    "site_crawl",
    "stores",
]
