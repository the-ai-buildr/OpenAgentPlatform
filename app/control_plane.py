"""Browser access from the AgentOS control plane at https://os.agno.com."""

from __future__ import annotations

from typing import Any


def enable_private_network_cors(app: Any) -> None:
    """Allow the control plane to call this API on localhost.

    Chrome sends ``Access-Control-Request-Private-Network: true`` before a
    public site may reach ``http://localhost``. Starlette rejects that
    preflight unless the CORS middleware was built with
    ``allow_private_network``. AgentOS does not set the flag, so the
    connection fails with ``Disallowed CORS private-network`` before ``/config``
    is ever requested.
    """
    from starlette.middleware.cors import CORSMiddleware

    changed = False
    for middleware in getattr(app, "user_middleware", []):
        if getattr(middleware, "cls", None) is CORSMiddleware:
            middleware.kwargs["allow_private_network"] = True
            changed = True
    if changed:
        app.middleware_stack = None
