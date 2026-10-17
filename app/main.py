"""OAP entrypoint.

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
    level=logging.DEBUG if settings.oap_debug else logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
)
logger = logging.getLogger("oap")

init_tracing()

from agno.job_queue import QueueConfig  # noqa: E402
from agno.os import AgentOS  # noqa: E402
from agno.os.settings import AgnoAPISettings  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from app.agents import runtime  # noqa: E402
from app.bridge import openwebui_sync  # noqa: E402
from app.bridge.openwebui_router import router as openwebui_router  # noqa: E402
from app.catalog_loader import start_watcher, stop_watcher  # noqa: E402
from app.control_plane import enable_private_network_cors  # noqa: E402
from app.knowledge import knowledge_manager as km  # noqa: E402
from app.knowledge.file_watcher import watcher_service  # noqa: E402
from app.tools.platform_tools import database_status  # noqa: E402

# Agno's scheduler defaults to port 7777; OAP serves on `oap_port`.
SCHEDULER_BASE_URL = f"http://127.0.0.1:{settings.oap_port}"


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
    openwebui_sync.ensure_synced_in_background(retries=5, delay_seconds=3.0)
    try:
        yield
    finally:
        stop_watcher()
        watcher_service.stop()


base_app = FastAPI(
    title="Open Agent Platform",
    description="Open Agent Platform with an OpenAI-compatible bridge for Open WebUI.",
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
    scheduler_running = _oap is not None and settings.enable_scheduler
    queue_running = _oap is not None and settings.enable_background_queue

    return {
        "status": "ok" if not degraded_reasons else "degraded",
        "oap_id": settings.oap_id,
        "oap_running": _oap is not None,
        "degraded_reasons": degraded_reasons,
        "llm": {
            "provider": "openrouter",
            "configured": settings.llm_configured,
            "default_model": settings.oap_default_model,
        },
        "embeddings": {
            "configured": km.embeddings_available(),
            "model": settings.embedding_model,
            "base_url": settings.embedding_base_url,
        },
        "database": {"reachable": db.get("reachable"), "pgvector": db.get("pgvector")},
        "telemetry": get_otel_status(),
        "watcher": {"running": watcher_service.running, "enabled": settings.enable_file_watcher},
        "scheduler": {
            "enabled": settings.enable_scheduler,
            "running": scheduler_running,
            "poll_interval_seconds": settings.scheduler_poll_interval,
            "base_url": SCHEDULER_BASE_URL,
        },
        "background": {
            "queue_enabled": queue_running,
            "durable": queue_running,
            "max_concurrency": settings.background_max_concurrency,
            "store_run_events": settings.store_run_events,
        },
        "endpoints": {
            "docs": "/docs",
            "health": "/health",
            "models": "/v1/models",
            "chat_completions": "/v1/chat/completions",
            "file_route": "/v1/files/route",
            "watcher_status": "/v1/files/watcher-status",
            "run_status": "/v1/runs/{run_id}",
            "run_cancel": "/v1/runs/{run_id}/cancel",
            "run_resolve": "/v1/runs/{run_id}/resolve",
            "schedules": "/schedules" if _oap is not None else None,
            "queue": "/queue" if queue_running else None,
            "mcp": "/mcp" if (_oap is not None and settings.enable_mcp_server) else None,
        },
    }


def _queue_config() -> QueueConfig | None:
    """Durable background queue stored in the AgentOS database (oap-db)."""
    if not settings.enable_background_queue:
        return None
    return QueueConfig(durable=True, max_concurrency=settings.background_max_concurrency)


def _build_oap() -> tuple[AgentOS, FastAPI]:
    runtime.initialize()
    oap = AgentOS(
        id=settings.oap_id,
        name="Open Agent Platform",
        description="Local Open Agent Platform. Connect os.agno.com to http://localhost:8000.",
        agents=runtime.get_agentos_agents(),
        teams=runtime.get_agentos_teams(),
        workflows=runtime.get_catalog_workflows(),
        knowledge=km.get_all_knowledge_list(),
        db=runtime.get_db(),
        registry=runtime.get_registry(),
        settings=AgnoAPISettings(os_security_key=settings.os_security_key.strip() or None),
        mcp=settings.enable_mcp_server,
        telemetry=settings.oap_telemetry,
        lifespan=lifespan,
        base_app=base_app,
        on_route_conflict="preserve_base_app",
        queue=_queue_config(),
        scheduler=settings.enable_scheduler,
        scheduler_poll_interval=settings.scheduler_poll_interval,
        scheduler_base_url=SCHEDULER_BASE_URL,
    )
    app = oap.get_app()
    enable_private_network_cors(app)
    return oap, app


_oap: AgentOS | None = None

if settings.llm_configured:
    _oap, app = _build_oap()
    _running = _oap

    def _resync() -> None:
        """Re-register dynamically created agents and projects with the running platform."""
        try:
            _running.agents = runtime.get_agentos_agents()
            _running.teams = runtime.get_agentos_teams()
            _running.workflows = runtime.get_catalog_workflows()
            _running.resync(app)
            logger.info("OAP resynced with %d agents", len(_running.agents or []))
            if settings.openwebui_auto_sync:
                openwebui_sync.ensure_synced_in_background()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("OAP resync failed: %s", exc)

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
