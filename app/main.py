"""AgentOS entrypoint.

Two ordering constraints are load-bearing here.

1. `init_tracing()` patches SQLAlchemy and HTTPX at class level, so it runs
   before any module that constructs an engine or client is imported.
2. The application's own routes are registered on a base FastAPI app that is
   handed to `AgentOS(base_app=...)`. AgentOS appends a catch-all `Mount` to
   whatever app it builds, so routes added *after* `get_app()` are never
   matched — and its own `/health` would shadow ours.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from app.settings import settings
from app.telemetry.otel import get_otel_status, init_tracing, instrument_app

logging.basicConfig(
    level=logging.DEBUG if settings.agno_debug else logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
)
logger = logging.getLogger("agentos")

init_tracing()

from agno.os import AgentOS  # noqa: E402
from agno.os.settings import AgnoAPISettings  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from app.agents import runtime  # noqa: E402
from app.bridge.openwebui_router import router as openwebui_router  # noqa: E402
from app.catalog_loader import start_watcher, stop_watcher  # noqa: E402
from app.control_plane import enable_private_network_cors  # noqa: E402
from app.knowledge import knowledge_manager as km  # noqa: E402
from app.knowledge.file_watcher import watcher_service  # noqa: E402
from app.tools.platform_tools import database_status  # noqa: E402


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Start and stop the workspace watcher alongside the API."""
    if settings.enable_file_watcher:
        try:
            watcher_service.start()
        except Exception as exc:  # pragma: no cover - depends on host filesystem
            logger.warning("File watcher could not start: %s", exc)
    else:
        logger.info("File watcher disabled via ENABLE_FILE_WATCHER")
    try:
        start_watcher(runtime.reload_dynamic_agents)
    except Exception as exc:  # pragma: no cover - depends on host filesystem
        logger.warning("Catalog watcher could not start: %s", exc)
    try:
        yield
    finally:
        stop_watcher()
        watcher_service.stop()


base_app = FastAPI(
    title="Agno AgentOS platform",
    description="Agno AgentOS with an OpenAI-compatible bridge for Open WebUI.",
    lifespan=lifespan,
)
base_app.include_router(openwebui_router)


@base_app.get("/health", tags=["platform"])
def health() -> dict[str, Any]:
    """Liveness and configuration report. Intentionally unauthenticated."""
    db = database_status()
    degraded_reasons: list[str] = []
    if not settings.llm_configured:
        degraded_reasons.append("OPENROUTER_API_KEY is not set")
    if not km.embeddings_available():
        degraded_reasons.append("EMBEDDING_API_KEY is not set (knowledge indexing disabled)")
    if not db.get("reachable"):
        degraded_reasons.append(f"database unreachable: {db.get('error')}")

    return {
        "status": "ok" if not degraded_reasons else "degraded",
        "agentos_id": settings.agentos_id,
        "agentos_running": _agent_os is not None,
        "degraded_reasons": degraded_reasons,
        "llm": {
            "provider": "openrouter",
            "configured": settings.llm_configured,
            "default_model": settings.agno_default_model,
        },
        "embeddings": {
            "configured": km.embeddings_available(),
            "model": settings.embedding_model,
            "base_url": settings.embedding_base_url,
        },
        "database": {"reachable": db.get("reachable"), "pgvector": db.get("pgvector")},
        "telemetry": get_otel_status(),
        "watcher": {"running": watcher_service.running, "enabled": settings.enable_file_watcher},
        "endpoints": {
            "docs": "/docs",
            "health": "/health",
            "models": "/v1/models",
            "chat_completions": "/v1/chat/completions",
            "file_route": "/v1/files/route",
            "watcher_status": "/v1/files/watcher-status",
            "mcp": "/mcp" if (_agent_os is not None and settings.enable_mcp_server) else None,
        },
    }


def _build_agent_os() -> tuple[AgentOS, FastAPI]:
    runtime.initialize()
    agent_os = AgentOS(
        id=settings.agentos_id,
        name="Open Agent Platform",
        description="Local AgentOS. Connect os.agno.com to http://localhost:8000.",
        agents=runtime.get_all_agents_list(),
        teams=[runtime.get_team(), *runtime.get_catalog_teams()],
        workflows=runtime.get_catalog_workflows(),
        knowledge=km.get_all_knowledge_list(),
        db=runtime.get_db(),
        registry=runtime.get_registry(),
        settings=AgnoAPISettings(os_security_key=settings.os_security_key.strip() or None),
        mcp=settings.enable_mcp_server,
        telemetry=settings.agno_telemetry,
        lifespan=lifespan,
        base_app=base_app,
        on_route_conflict="preserve_base_app",
    )
    app = agent_os.get_app()
    enable_private_network_cors(app)
    return agent_os, app


_agent_os: AgentOS | None = None

if settings.llm_configured:
    _agent_os, app = _build_agent_os()
    _running_os = _agent_os

    def _resync() -> None:
        """Re-register dynamically created agents with the running AgentOS."""
        try:
            _running_os.agents = runtime.get_all_agents_list()
            _running_os.teams = [runtime.get_team(), *runtime.get_catalog_teams()]
            _running_os.workflows = runtime.get_catalog_workflows()
            _running_os.resync(app)
            logger.info("AgentOS resynced with %d agents", len(_running_os.agents or []))
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("AgentOS resync failed: %s", exc)

    runtime.on_agents_changed(_resync)
else:
    # Without an LLM key no Agno model can be constructed, so the full AgentOS
    # cannot boot. Serving the base app alone keeps /health, /v1/models and the
    # container healthcheck working, so the operator sees *why* it is degraded
    # instead of a crash loop.
    logger.error(
        "OPENROUTER_API_KEY is not set - starting in degraded mode. "
        "Agents, the team and /mcp are unavailable until a key is configured."
    )
    app = base_app

instrument_app(app)
