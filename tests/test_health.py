"""Liveness and readiness endpoints (no DB or Redis required)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.auth import PUBLIC_PATHS
from app.main import app


def test_livez_is_public():
    assert "/livez" in PUBLIC_PATHS


def test_livez_answers_without_a_database():
    # No lifespan here, so there is no database pool at all.
    client = TestClient(app)
    response = client.get("/livez")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_hides_the_database_error():
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json() == {"detail": "db unavailable"}
