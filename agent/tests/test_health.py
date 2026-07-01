"""Smoke tests for the REST surface (lifespan boots the full runtime)."""

from fastapi.testclient import TestClient

from app.main import app


def test_healthz_ok():
    with TestClient(app) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in ("ok", "starting")
    assert "version" in body


def test_fraud_endpoint_requires_token():
    with TestClient(app) as client:
        resp = client.post("/v1/tasks/fraud", json={"odoo_task_ref": "AIOPS/1", "order": {}})
    assert resp.status_code == 401


def test_fraud_endpoint_accepts_valid_token(monkeypatch):
    # Don't actually run the LLM workflow - stub the runtime entry point.
    with TestClient(app) as client:

        async def _fake_start(req, run_id=None):
            return run_id

        monkeypatch.setattr(client.app.state.runtime, "start_fraud", _fake_start)
        resp = client.post(
            "/v1/tasks/fraud",
            headers={"Authorization": "Bearer testtoken"},
            json={
                "odoo_task_ref": "AIOPS/2026/00001",
                "odoo_task_id": 1,
                "risk_level": "high",
                "order": {"total": 250, "currency": "USD"},
            },
        )
    assert resp.status_code == 202
    body = resp.json()
    assert body["run_id"].startswith("fr-")
    assert body["status"] == "accepted"
