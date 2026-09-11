import pytest
from fastapi.testclient import TestClient

from app import version as version_module
from app.version import get_version


def test_root_reports_name_and_version(client: TestClient):
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"name": "hivtools-mcp", "version": get_version()}


def test_version_endpoint(client: TestClient):
    response = client.get("/version")
    assert response.status_code == 200
    assert response.json() == {"name": "hivtools-mcp", "version": get_version()}
    assert response.json()["version"] != "unknown"


def test_health_is_ok(client: TestClient):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_ready_checks_the_database(client: TestClient):
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_get_version_falls_back_to_unknown_when_pyproject_missing(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(version_module, "_PYPROJECT", tmp_path / "nope.toml")
    version_module._pyproject.cache_clear()
    try:
        assert version_module.get_version() == "unknown"
        assert version_module.get_name() == "hivtools-mcp"
    finally:
        version_module._pyproject.cache_clear()
