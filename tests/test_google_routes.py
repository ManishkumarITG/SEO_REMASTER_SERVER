"""Google OAuth routes: auth gating and state validation.

No network calls - every request below fails before any Google request is made.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

import main


def _client() -> TestClient:
    # Context-managed so the lifespan runs (creates tables).
    return TestClient(main.app)


def test_status_reports_not_connected_without_session() -> None:
    with _client() as client:
        response = client.get("/auth/status")
        assert response.status_code == 200
        assert response.json() == {"connected": False}


def test_sites_requires_connection() -> None:
    with _client() as client:
        assert client.get("/api/sites").status_code == 401


def test_search_analytics_requires_connection() -> None:
    with _client() as client:
        response = client.get(
            "/api/search-analytics",
            params={
                "siteUrl": "https://example.com/",
                "startDate": "2026-01-01",
                "endDate": "2026-01-28",
            },
        )
        assert response.status_code == 401


def test_callback_rejects_forged_state() -> None:
    with _client() as client:
        response = client.get("/auth/callback", params={"state": "forged", "code": "x"})
        assert response.status_code == 400
        assert "state" in response.json()["detail"].lower()
