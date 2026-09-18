"""HTTP routers.

Registration order is preserved exactly as main.py had it: health, audits,
google_auth, search_console, reddit_auth, backlinks. Order matters for path
resolution, so keep it.
"""

from __future__ import annotations

from fastapi import FastAPI

from routes import audits, backlinks, google_auth, health, reddit_auth, search_console


def register_routes(app: FastAPI) -> None:
    app.include_router(health.router)
    app.include_router(audits.router)
    app.include_router(google_auth.router)
    app.include_router(search_console.router)
    app.include_router(reddit_auth.router)
    app.include_router(backlinks.router)


__all__ = [
    "audits",
    "backlinks",
    "google_auth",
    "health",
    "reddit_auth",
    "register_routes",
    "search_console",
]
