"""Application startup, health and provider-status tests."""

from __future__ import annotations


def test_health_ok(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["mode"] == "mock"
    assert body["version"]


def test_root_endpoint(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["name"] == "Svarah API"


def test_root_health_alias(client):
    """Monitoring probes `/health`; it must answer like `/api/health`."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_modes_reports_providers(client):
    response = client.get("/api/modes")
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "mock"

    names = {p["name"] for p in body["providers"]}
    assert {"agnes_llm", "agnes_image", "agnes_video", "voice", "publisher"} <= names
    # Unverified providers must never advertise availability.
    agnes = next(p for p in body["providers"] if p["name"] == "agnes_llm")
    assert agnes["verified"] is False
    assert agnes["available"] is False

    assert body["stt"]["name"] == "stt"
    assert body["stt"]["available"] is True  # mock provider active
    assert body["database"]["status"] == "ok"
    assert "root" in body["storage"]


def test_modes_hides_db_credentials(client, env_override):
    env_override(TITAN_DATABASE_URL="postgresql://user:secret@localhost:5432/titan")
    response = client.get("/api/modes")
    assert response.status_code == 200
    db_url = response.json()["database"]["url"]
    assert "secret" not in db_url
    assert "***" in db_url
