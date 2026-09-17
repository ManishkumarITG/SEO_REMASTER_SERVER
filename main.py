"""FastAPI application entry point.

The ASGI target is ``main:app`` - *not* ``app.main:app``. ``app/`` is a plain
directory, not a package; ``app/server`` is the import root (every import here is
top-level: ``from services...``, ``from config import ...``).

Three ways to start it, in order of how little can go wrong:

  python main.py                        # any cwd - production launch
  uvicorn --app-dir /path/to/app/server main:app --host 0.0.0.0 --port 8000
  uvicorn main:app --reload             # dev, from app/server only
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from api.routes import audits, backlinks, google_auth, health, reddit_auth, search_console
from config import get_settings, unknown_env_keys
from db.session import init_db
from services.integrations.mongo import close_client as close_mongo_client
from workers.tasks import reconcile_orphaned_audits

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    ignored = unknown_env_keys()
    if ignored:
        # Settings ignores unknown keys, so a misspelled one disables a feature in
        # total silence. Name them once at startup instead.
        logging.getLogger("seo").warning(
            "Ignoring %d unrecognised key(s) in app/server/.env: %s - "
            "these match no setting in config.py and have no effect.",
            len(ignored), ", ".join(ignored),
        )
    await init_db()  # dev convenience: auto-create tables (Alembic comes later)
    # Background tasks die with the process, so anything left "running" by the
    # previous run (or the last --reload) will never finish. Close those out.
    await reconcile_orphaned_audits()
    yield
    await close_mongo_client()


app = FastAPI(title="SEO Analyzer", version="0.1.0", lifespan=lifespan)

settings = get_settings()

session_secret = settings.session_secret
if not session_secret:
    # The app must boot with zero keys set, so fall back to an ephemeral secret.
    # Google Search Console connections then reset on every restart -
    # set SESSION_SECRET in app/server/.env to persist them.
    session_secret = secrets.token_hex(32)
    logging.getLogger("seo").warning(
        "SESSION_SECRET is not set - using an ephemeral session secret. "
        "Google Search Console connections will not survive a server restart."
    )

# Middleware order: the LAST middleware added runs FIRST, so sessions are added
# before CORS to keep CORS outermost (headers reach the browser even on errors).
app.add_middleware(
    SessionMiddleware,
    secret_key=session_secret,
    same_site="lax",  # localhost:5173 -> localhost:8000 is same-SITE, so the cookie flows
    https_only=False,  # set True when serving over HTTPS in production
    max_age=60 * 60 * 24 * 30,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_origin_regex=settings.cors_origin_regex or None,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(audits.router)
app.include_router(google_auth.router)
app.include_router(search_console.router)
app.include_router(reddit_auth.router)
app.include_router(backlinks.router)


if __name__ == "__main__":
    # Running this file as a script puts its directory on sys.path automatically,
    # so the server starts from ANY working directory. That makes this the launch
    # path for production, where the cwd is whatever the process manager chose.
    # The app object is passed directly (not the "main:app" string) so no import
    # by name happens in a reloader/worker subprocess that may have a different cwd.
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)
