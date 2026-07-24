"""Shared-token auth on the agent's REST surface.

``test_health.py`` covers the two happy-ish cases through the fraud endpoint (no
header -> 401, right token -> 202). What it leaves open is everything that makes
``require_bearer`` a *security* control rather than a presence check: a wrong
token, a header that isn't in ``Bearer <token>`` form, a near-miss token, and the
unconfigured-secret branch. It also never touches ``/v1/tasks/reconciliation``,
which is guarded by the same router-level dependency.

The guard is deliberately strict: the token is only ever read from a header that
starts with the exact string ``"Bearer "``, so anything else yields an empty
candidate and fails the constant-time compare. These tests pin that.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import security
from app.main import app

# Matches AI_OPS_SHARED_TOKEN in conftest.py.
_TOKEN = "testtoken"

_FRAUD_BODY = {
    "odoo_task_ref": "AIOPS/2026/00002",
    "odoo_task_id": 1,
    "risk_level": "high",
    "order": {"total": 250, "currency": "USD"},
}
_RECON_BODY = {"odoo_task_ref": "AIOPS/2026/00003", "odoo_task_id": 1, "product_id": 1}

# (path, body, run_id prefix the endpoint mints)
_PROTECTED = [
    ("/v1/tasks/fraud", _FRAUD_BODY, "fr-"),
    ("/v1/tasks/reconciliation", _RECON_BODY, "rc-"),
]


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Rejected credentials
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path,body,_prefix", _PROTECTED)
@pytest.mark.parametrize(
    "header",
    [
        pytest.param(None, id="absent"),
        pytest.param("", id="empty"),
        pytest.param("Bearer wrong-token", id="wrong-token"),
        pytest.param("Bearer ", id="bearer-no-token"),
        # No scheme: the raw token must NOT authenticate.
        pytest.param(_TOKEN, id="no-scheme"),
        # Scheme match is case-sensitive by construction.
        pytest.param(f"bearer {_TOKEN}", id="lowercase-scheme"),
        pytest.param(f"BEARER {_TOKEN}", id="uppercase-scheme"),
        # A different scheme carrying the right value.
        pytest.param(f"Basic {_TOKEN}", id="wrong-scheme"),
        # Near-misses: prefix, suffix and stray whitespace must all fail.
        pytest.param(f"Bearer {_TOKEN[:-1]}", id="truncated"),
        pytest.param(f"Bearer {_TOKEN}x", id="extra-char"),
        pytest.param(f"Bearer  {_TOKEN}", id="double-space"),
        pytest.param(f"Bearer {_TOKEN} ", id="trailing-space"),
    ],
)
def test_protected_endpoints_reject_bad_credentials(client, path, body, _prefix, header):
    headers = {} if header is None else {"Authorization": header}
    resp = client.post(path, headers=headers, json=body)
    assert resp.status_code == 401, f"{path} accepted {header!r} -> {resp.status_code}"


# ---------------------------------------------------------------------------
# Accepted credentials
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path,body,prefix", _PROTECTED)
def test_protected_endpoints_accept_the_shared_token(client, monkeypatch, path, body, prefix):
    """The valid token reaches the handler - on *both* task endpoints.

    Also pins the 202 contract Odoo relies on: it stores ``run_id`` on the
    ``ai.ops.task`` record, so the id must come back synchronously and be
    recognisable per workflow. The runtime entry points are stubbed so no LLM
    workflow actually runs.
    """

    async def _fake_start(req, run_id=None):
        return run_id

    monkeypatch.setattr(client.app.state.runtime, "start_fraud", _fake_start)
    monkeypatch.setattr(client.app.state.runtime, "start_reconciliation", _fake_start)

    resp = client.post(path, headers={"Authorization": f"Bearer {_TOKEN}"}, json=body)
    assert resp.status_code == 202, resp.text
    payload = resp.json()
    assert payload["run_id"].startswith(prefix), payload
    assert payload["status"] == "accepted", payload
    assert payload["odoo_task_ref"] == body["odoo_task_ref"]


# ---------------------------------------------------------------------------
# Misconfiguration: an unset secret must fail closed, and say so distinctly
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path,body,_prefix", _PROTECTED)
def test_unconfigured_shared_token_fails_closed_with_503(
    client, monkeypatch, path, body, _prefix
):
    """With no token configured, nothing authenticates - not even an empty header.

    503 (not 401) is the point: an unconfigured agent is broken, not a caller
    presenting bad credentials, and the two need to be distinguishable in logs.
    A ``compare_digest("", "")`` would otherwise succeed and let *everyone* in.
    """
    blank = security.get_settings().model_copy(update={"ai_ops_shared_token": ""})
    monkeypatch.setattr(security, "get_settings", lambda: blank)

    for header in ({"Authorization": f"Bearer {_TOKEN}"}, {"Authorization": "Bearer "}, {}):
        resp = client.post(path, headers=header, json=body)
        assert resp.status_code == 503, f"{path} with {header!r} -> {resp.status_code}"
