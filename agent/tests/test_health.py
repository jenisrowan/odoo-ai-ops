"""Smoke test for the REST surface (lifespan boots the full runtime).

Authentication on the task endpoints - including the 202 response contract they
return once a caller is through the guard - lives in ``test_security.py``.
"""

from fastapi.testclient import TestClient

from app.main import app


def test_healthz_ok():
    with TestClient(app) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in ("ok", "starting")
    assert "version" in body
