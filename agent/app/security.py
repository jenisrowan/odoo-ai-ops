"""Shared-token authentication for the agent's REST API.

Odoo authenticates to the agent with the same bearer token the agent uses when
calling back into Odoo (``AI_OPS_SHARED_TOKEN``). Compared in constant time.
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from .config import get_settings


async def require_bearer(authorization: str = Header(default="")) -> None:
    expected = get_settings().ai_ops_shared_token
    if not expected:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="agent shared token not configured"
        )
    token = authorization[len("Bearer ") :] if authorization.startswith("Bearer ") else ""
    if not hmac.compare_digest(token, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid token")
