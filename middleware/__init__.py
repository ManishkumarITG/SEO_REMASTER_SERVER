"""Middleware registration.

Moved out of main.py unchanged. The **order is load-bearing** and is preserved
exactly: the LAST middleware added runs FIRST, so SessionMiddleware is added
before CORSMiddleware to keep CORS outermost (its headers then reach the browser
even on error responses).
"""

from __future__ import annotations

import logging
import secrets

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from config import get_settings


def register_middleware(app: FastAPI) -> None:
    settings = get_settings()

    session_secret = settings.session_secret
    if not session_secret:
        # The app must boot with zero keys set, so fall back to an ephemeral secret.
        # Google Search Console connections then reset on every restart -
        # set SESSION_SECRET in server/.env to persist them.
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


__all__ = ["register_middleware"]
