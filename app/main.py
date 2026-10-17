"""AgentOS entrypoint.

Import order matters here. `init_tracing()` patches SQLAlchemy and HTTPX at
class level, so it runs before any module that constructs an engine or client
is imported. Everything below that line is a normal import.
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
from fastapi import FastAPI  # noqa: E402

from app.agents import runtime  # noqa: E402
from app.bridge.openwebui_router import router as openwebui_router  # noqa: E402
from app.knowledge import knowledge_manager as km  # noqa: E402
from app.knowledge.file_watcher import watcher_service  # noqa: E402
from app.tools.platform_tools import database_status  # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start and stop the workspace watcher alongside the API."""
    if settings.enable_file_watcher:
        try:
            watcher_service.start()
        except Exception as exc:  # pragma: no cover - depends on host filesystem
            logger.warning("File watcher could not start: %s", exc)
    else:
        logger.info("File watcher disabled via ENABLE_FILE_WATCHER")
    try:
        yield
    finally:
        watcher_service.stop()


def _build_agent_os() -> tuple[AgentOS, FastAPI]:
    runtime.initialize()
    agent_os = AgentOS(
        id=settings.agentos_id,
        agents=runtime.get_all_agents_list(),
        teams=[runtime.get_team()],
        knowledge=km.get_all_knowledge_list(),
        db=runtime._get_db(),
        mcp=settings.enable_mcp_server,
        telemetry=settings.agno_telemetry,
        lifespan=lifespan,
    )
    return agent_os, agent_os.get_app()


if settings.llm_configured:
    agent_os, app = _build_agent_os()

    def _resync() -> None:
        """Re-register dynamically created agents with the running AgentOS."""
        try:
            agent_os.agents = runtime.get_all_agents_list()
            agent_os.resync(app)
            logger.info("AgentOS resynced with %d agents", len(agent_os.agents or []))
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("AgentOS resync failed: %s", exc)

    runtime.on_agents_changed(_resync)
else:
    # Without an LLM key no Agno model can be constructed, so the full AgentOS
    # cannot boot. Serving a degraded app keeps /health, /v1/models and the
    # container healthcheck working so the operator sees *why* it is degraded
    # instead of a crash loop.
    logger.error(
        "OPENROUTER_API_KEY is not set - starting in degraded mode. "
        "Agents, the team and /mcp are unavailable until a key is configured."
    )
    agent_os = None  # type: ignore[assignment]
    app = FastAPI(
        title="Agno AgentOS (degraded)",
        description="OPENROUTER_API_KEY is not configured; agent endpoints are unavailable.",
        lifespan=lifespan,
    )

app.include_router(openwebui_router)
instrument_app(app)


@app.get("/health", tags=["platform"])
def health() -> dict[str, Any]:
    """Liveness and configuration report. Intentionally unauthenticated."""
    llm_ok = settings.llm_configured
    db = database_status()
    degraded_reasons: list[str] = []
    if not llm_ok:
        degraded_reasons.append("OPENROUTER_API_KEY is not set")
    if not km.embeddings_available():
        degraded_reasons.append("EMBEDDING_API_KEY is not set (knowledge indexing disabled)")
    if not db.get("reachable"):
        degraded_reasons.append(f"database unreachable: {db.get('error')}")

    return {
        "status": "ok" if not degraded_reasons else "degraded",
        "agentos_id": settings.agentos_id,
        "agentos_running": agent_os is not None,
        "degraded_reasons": degraded_reasons,
        "llm": {
            "provider": "openrouter",
            "configured": llm_ok,
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
            "mcp": "/mcp" if (agent_os is not None and settings.enable_mcp_server) else None,
        },
    }
