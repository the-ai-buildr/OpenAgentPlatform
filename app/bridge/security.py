"""Bearer-token authentication for the OpenAI-compatible bridge.

Open WebUI already sends `OS_SECURITY_KEY` as a bearer token on every request,
so checking it here costs nothing and closes the hole where `/v1/*` was
reachable unauthenticated. AgentOS installs its own key middleware too; this is
the explicit, independently testable layer for the routes this app adds.
"""

from __future__ import annotations

import hmac
import logging
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from app.settings import settings

logger = logging.getLogger(__name__)

_WARNED = False


def _presented_token(authorization: str | None, api_key_header: str | None) -> str | None:
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token.strip():
            return token.strip()
    if api_key_header and api_key_header.strip():
        return api_key_header.strip()
    return None


def require_bridge_auth(
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
) -> None:
    """Reject requests that do not present the configured security key.

    When `OS_SECURITY_KEY` is empty the bridge stays open and logs a warning
    once — that is the documented local-development mode, and the production
    compose file refuses to start without a key.
    """
    global _WARNED
    expected = settings.os_security_key.strip()
    if not expected:
        if not _WARNED:
            logger.warning(
                "OS_SECURITY_KEY is empty - the /v1 bridge is unauthenticated. "
                "Set it before exposing this service beyond localhost."
            )
            _WARNED = True
        return

    presented = _presented_token(authorization, x_api_key)
    if presented is None or not hmac.compare_digest(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "Bearer"},
        )


BridgeAuth = Depends(require_bridge_auth)
