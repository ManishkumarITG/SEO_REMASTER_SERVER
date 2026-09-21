"""FastAPI application entry point - bootstrap only.

The ASGI target is ``main:app``. ``server/`` is the import root (every import
here is top-level: ``from routes...``, ``from config import ...``).

Three ways to start it, in order of how little can go wrong:

  python main.py                        # any cwd - production launch
  uvicorn --app-dir /path/to/server main:app --host 0.0.0.0 --port 8000
  uvicorn main:app --reload             # dev, from server/ only

Request pipeline: routes -> controller -> services -> integrations / db.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from config import get_settings, placeholder_env_values, unknown_env_keys
from db.mongo import close_client as close_mongo_client
from db.session import init_db
from middleware import register_middleware
from routes import register_routes
from workers.tasks import reconcile_orphaned_audits, reconcile_orphaned_crawls

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    ignored = unknown_env_keys()
    if ignored:
        # Settings ignores unknown keys, so a misspelled one disables a feature in
        # total silence. Name them once at startup instead.
        logging.getLogger("seo").warning(
            "Ignoring %d unrecognised key(s) in server/.env: %s - "
            "these match no setting in config/settings.py and have no effect.",
            len(ignored), ", ".join(ignored),
        )
    placeholders = placeholder_env_values()
    if placeholders:
        # A recognised key holding a template value boots "successfully" and fails
        # later with an error that never mentions .env - Google's
        # redirect_uri_mismatch page, for one. Name the keys here instead. Never values.
        logging.getLogger("seo").warning(
            "%d setting(s) in server/.env still hold template placeholders: %s - "
            "the feature each one configures will fail until it is filled in.",
            len(placeholders), ", ".join(placeholders),
        )
    await init_db()  # dev convenience: auto-create tables (Alembic comes later)
    # Background tasks die with the process, so anything left "running" by the
    # previous run (or the last --reload) will never finish. Close those out.
    await reconcile_orphaned_audits()
    await reconcile_orphaned_crawls()
    yield
    await close_mongo_client()


app = FastAPI(title="SEO Analyzer", version="0.1.0", lifespan=lifespan)

settings = get_settings()

register_middleware(app)
register_routes(app)


if __name__ == "__main__":
    # Running this file as a script puts its directory on sys.path automatically,
    # so the server starts from ANY working directory. That makes this the launch
    # path for production, where the cwd is whatever the process manager chose.
    # The app object is passed directly (not the "main:app" string) so no import
    # by name happens in a reloader/worker subprocess that may have a different cwd.
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)
