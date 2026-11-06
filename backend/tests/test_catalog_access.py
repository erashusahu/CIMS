from unittest.mock import AsyncMock

import pytest
from app import main
from app.core.config import settings
from app.db.base import get_session
from fastapi.testclient import TestClient


@pytest.fixture
def local_client(monkeypatch):
    monkeypatch.setattr(settings, "catalog_local_mode", True)
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "frontend_origin", "http://127.0.0.1:5173")
    session = AsyncMock()

    async def override_session():
        yield session

    main.app.dependency_overrides[get_session] = override_session
    with TestClient(main.app, base_url="http://127.0.0.1", client=("127.0.0.1", 50123)) as client:
        yield client, session
    main.app.dependency_overrides.pop(get_session, None)


def test_local_catalog_is_disabled_outside_development(local_client, monkeypatch):
    client, session = local_client
    monkeypatch.setattr(settings, "app_env", "production")
    response = client.get("/api/v1/catalog/local-workspaces")
    assert response.status_code == 404
    session.execute.assert_not_awaited()


def test_local_catalog_rejects_dns_rebinding_host(local_client):
    client, session = local_client
    response = client.get("/api/v1/catalog/local-workspaces", headers={"Host": "external.example"})
    assert response.status_code == 403
    session.execute.assert_not_awaited()


def test_local_catalog_rejects_external_browser_origin(local_client):
    client, session = local_client
    response = client.get(
        "/api/v1/catalog/local-workspaces", headers={"Origin": "https://external.example"}
    )
    assert response.status_code == 403
    session.execute.assert_not_awaited()


def test_local_catalog_requires_origin_for_mutations(local_client):
    client, session = local_client
    response = client.post(
        "/api/v1/catalog/local-workspaces", json={"name": "Acme", "slug": "acme"}
    )
    assert response.status_code == 403
    session.execute.assert_not_awaited()


def test_local_catalog_requires_explicit_workspace(local_client):
    client, session = local_client
    response = client.get("/api/v1/catalog/items")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "workspace_required"
    session.execute.assert_not_awaited()


def test_local_catalog_rejects_non_loopback_client(monkeypatch):
    monkeypatch.setattr(settings, "catalog_local_mode", True)
    monkeypatch.setattr(settings, "app_env", "development")
    with TestClient(
        main.app, base_url="http://127.0.0.1", client=("192.168.1.30", 50123)
    ) as client:
        response = client.get("/api/v1/catalog/local-status")
    assert response.status_code == 403


def test_catalog_requires_authentication_when_local_mode_is_disabled(local_client, monkeypatch):
    client, session = local_client
    monkeypatch.setattr(settings, "catalog_local_mode", False)
    response = client.get("/api/v1/catalog/items")
    assert response.status_code == 401
    session.execute.assert_not_awaited()
