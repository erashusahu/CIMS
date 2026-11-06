import logging
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError, StatementError

from app import main
from app.db.base import get_session
from app.db.readiness import ReadinessIssue, ReadinessReport


@pytest.fixture
def client():
    # These HTTP-contract tests never open a database connection.
    with TestClient(main.app, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def failing_session():
    session = AsyncMock()

    async def override_session():
        yield session

    main.app.dependency_overrides[get_session] = override_session
    yield session
    main.app.dependency_overrides.pop(get_session, None)


def assert_error(response, status, code, request_id="foundation-test"):
    assert response.status_code == status
    assert response.headers["X-Request-ID"] == request_id
    error = response.json()["error"]
    assert error["code"] == code
    assert error["request_id"] == request_id
    assert error["message"]
    return error


@pytest.mark.parametrize("mismatched", [False, True])
def test_registration_validation_returns_safe_json(client, failing_session, mismatched):
    password = "private-value-do-not-echo"
    payload = {
        "business_name": "Acme",
        "business_identifier": "acme-traders" if mismatched else "INVALID ID",
        "owner_name": "Owner",
        "email": "owner@example.com",
        "password": password,
        "password_confirmation": "different-private-value" if mismatched else password,
        "profile": "retail",
    }
    response = client.post(
        "/api/v1/auth/register", json=payload, headers={"X-Request-ID": "foundation-test"}
    )
    error = assert_error(response, 422, "validation_error")
    assert error["fields"]
    assert all(set(field) == {"loc", "msg", "type"} for field in error["fields"])
    assert password not in response.text
    assert payload["password_confirmation"] not in response.text
    failing_session.execute.assert_not_awaited()


@pytest.mark.parametrize(
    ("method", "path", "status", "code"),
    [("get", "/missing", 404, "not_found"), ("post", "/health", 405, "method_not_allowed")],
)
def test_framework_errors_use_the_api_contract(client, method, path, status, code):
    response = getattr(client, method)(path, headers={"X-Request-ID": "foundation-test"})
    assert_error(response, status, code)
    if status == 405:
        assert "GET" in response.headers["Allow"]


@pytest.mark.parametrize("unexpected", [False, True])
def test_server_errors_hide_sql_and_credentials(client, failing_session, caplog, unexpected):
    secret = "private-database-parameter"
    failure = (
        RuntimeError(secret)
        if unexpected
        else StatementError(
            "database details", "SELECT private_sql", {"secret": secret}, RuntimeError(secret)
        )
    )
    failing_session.execute.side_effect = failure
    with caplog.at_level(logging.ERROR, logger="cims.errors"):
        response = client.post(
            "/api/v1/auth/login",
            json={
                "business_identifier": "acme-traders",
                "email": "owner@example.com",
                "password": "private-password",
            },
            headers={"X-Request-ID": "foundation-test"},
        )
    assert_error(response, 500, "internal_error" if unexpected else "database_error")
    assert secret not in response.text + caplog.text
    assert "private_sql" not in response.text + caplog.text
    assert any(
        record.request_id == "foundation-test"
        for record in caplog.records
        if record.name == "cims.errors"
    )


def test_health_is_independent_of_database_readiness(client, monkeypatch):
    check = AsyncMock(side_effect=AssertionError("health must not access PostgreSQL"))
    monkeypatch.setattr(main, "check_database_readiness", check)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    check.assert_not_awaited()


def test_readiness_reports_success_only_for_a_ready_database(client, monkeypatch):
    check = AsyncMock(return_value=ReadinessReport(migration="0001_foundation"))
    monkeypatch.setattr(main, "check_database_readiness", check)
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "migration": "0001_foundation"}


@pytest.mark.parametrize(
    "issue",
    [
        "runtime_role_unsafe",
        "runtime_role_owns_tables",
        "foundation_tables_missing",
        "foundation_columns_missing",
        "migration_not_current",
    ],
)
def test_readiness_rejects_incomplete_setup_without_exposing_details(client, monkeypatch, issue):
    report = ReadinessReport(issues=[ReadinessIssue(issue, "private schema details")])
    monkeypatch.setattr(main, "check_database_readiness", AsyncMock(return_value=report))
    response = client.get("/ready", headers={"X-Request-ID": "foundation-test"})
    assert_error(response, 503, "database_not_ready")
    assert "private schema details" not in response.text


@pytest.mark.parametrize("failure", [SQLAlchemyError, ConnectionRefusedError, TimeoutError])
def test_readiness_handles_database_failure(client, monkeypatch, failure):
    monkeypatch.setattr(
        main,
        "check_database_readiness",
        AsyncMock(side_effect=failure("private connection details")),
    )
    response = client.get("/ready", headers={"X-Request-ID": "foundation-test"})
    assert_error(response, 503, "database_unavailable")
    assert "private connection details" not in response.text
