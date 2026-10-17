"""Tools the platform agents use to inspect and change the running stack.

Every function here is a plain callable with a docstring — Agno turns those
into tool schemas. The module depends on `app.agents.store` but never on
`app.agents.runtime`, so there is no import cycle; `runtime` subscribes to the
store for reloads instead.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.agents.store import (
    RESERVED_AGENT_IDS,
    AgentSpec,
    RegistryError,
    bind_folder_to_agent_if_custom,
    delete_spec,
    load_specs,
    resolve_agent_target_folder,
    upsert_spec,
)
from app.knowledge import knowledge_manager as km
from app.knowledge.file_watcher import watcher_service
from app.knowledge.paths import UnsafePathError, folder_to_table_name, normalise_folder
from app.settings import PROJECT_ROOT, WORKSPACE_ROOT, settings
from app.telemetry.otel import get_otel_status

logger = logging.getLogger(__name__)

# `read_source_file` is deliberately restricted to the application's own source
# trees. Pointing it at /app would expose any mounted secret file.
READABLE_SOURCE_ROOTS = (PROJECT_ROOT / "app", PROJECT_ROOT / "openwebui_functions")
READABLE_SOURCE_SUFFIXES = frozenset({".py", ".json", ".md", ".sql", ".yml", ".yaml", ".toml"})
MAX_SOURCE_READ_BYTES = 200_000


def _error(exc: Exception) -> dict[str, Any]:
    return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}


# --------------------------------------------------------------------------- #
# Workspace files
# --------------------------------------------------------------------------- #
def create_workspace_directory(folder: str) -> dict[str, Any]:
    """Create a folder under workspace_files/ and return its PgVector table name.

    Use this before routing documents to a new topic area. Do not use it to
    create folders outside the workspace; paths that escape it are rejected.
    """
    try:
        directory = km.ensure_folder(folder)
        key = normalise_folder(folder)
        return {
            "status": "ok",
            "folder": key,
            "path": str(directory),
            "pgvector_table": folder_to_table_name(key),
        }
    except (UnsafePathError, OSError) as exc:
        return _error(exc)


def list_workspace_directories_and_files() -> dict[str, Any]:
    """List every workspace folder, the files in it, and recent Open WebUI uploads."""
    folders: dict[str, Any] = {}
    for key in km.known_folders():
        directory = km.ensure_folder(key)
        folders[key] = {
            "pgvector_table": folder_to_table_name(key),
            "files": sorted(p.name for p in directory.iterdir() if p.is_file()),
        }
    return {
        "status": "ok",
        "workspace_root": str(WORKSPACE_ROOT),
        "folders": folders,
        "recent_openwebui_uploads": km.list_openwebui_uploads(),
    }


def save_text_file_to_folder(
    folder: str, filename: str, content: str, auto_index: bool = True
) -> dict[str, Any]:
    """Write a text/markdown document into a workspace folder and index it.

    Use this for notes the agent itself authors. Do not use it for binary
    formats such as PDFs — route those from an Open WebUI upload instead.
    """
    try:
        return {"status": "ok", **km.save_text_file(folder, filename, content, auto_index)}
    except (UnsafePathError, OSError, ValueError) as exc:
        return _error(exc)


def route_openwebui_upload_to_folder(
    filename_query: str,
    target_folder: str | None = None,
    agent_id: str | None = None,
    auto_index: bool = True,
) -> dict[str, Any]:
    """Copy a file the user uploaded in Open WebUI into a workspace folder.

    `filename_query` is matched against the newest upload whose stored name
    contains it. When `target_folder` is omitted the folder bound to `agent_id`
    is used.
    """
    try:
        folder = resolve_agent_target_folder(agent_id, target_folder)
        result = km.stage_openwebui_upload(filename_query, folder, auto_index=auto_index)
        result["bound_to_agent"] = bind_folder_to_agent_if_custom(agent_id, folder)
        return {"status": "ok", **result}
    except (FileNotFoundError, UnsafePathError, ValueError) as exc:
        return _error(exc)


def sync_folder_to_pgvector(folder: str, recreate: bool = False) -> dict[str, Any]:
    """Re-index every supported file in a workspace folder.

    Set `recreate=True` only when the folder's embeddings must be rebuilt from
    scratch; it drops and refills the folder's vector table.
    """
    try:
        return {"status": "ok", **km.sync_folder(folder, recreate=recreate)}
    except (UnsafePathError, ValueError) as exc:
        return _error(exc)


def inspect_file_watcher_status() -> dict[str, Any]:
    """Report whether the workspace file watcher is running and what it indexed."""
    return {"status": "ok", **watcher_service.status()}


# --------------------------------------------------------------------------- #
# Agent registry
# --------------------------------------------------------------------------- #
def create_or_update_agent(
    agent_id: str,
    name: str,
    role: str = "",
    instructions: str = "",
    model_id: str | None = None,
    knowledge_folder: str | None = None,
    enable_web_search: bool = False,
) -> dict[str, Any]:
    """Create a new specialist agent, or update one that already exists.

    Core platform agents cannot be replaced. Omitting `knowledge_folder` on an
    update keeps the agent's existing folder binding.
    """
    try:
        spec = upsert_spec(
            AgentSpec(
                agent_id=agent_id.strip(),
                name=name.strip() or agent_id.strip(),
                role=role,
                instructions=instructions,
                model_id=model_id,
                knowledge_folder=knowledge_folder,
                enable_web_search=enable_web_search,
            )
        )
        return {"status": "ok", "agent": spec.to_dict()}
    except (RegistryError, UnsafePathError, OSError) as exc:
        return _error(exc)


def delete_custom_agent(agent_id: str) -> dict[str, Any]:
    """Remove a custom agent from the registry. Core agents cannot be deleted."""
    if agent_id in RESERVED_AGENT_IDS:
        return {"status": "error", "error": f"'{agent_id}' is a core platform agent"}
    removed = delete_spec(agent_id)
    return {"status": "ok" if removed else "not_found", "agent_id": agent_id}


def list_registered_agents() -> dict[str, Any]:
    """List the core platform agents and every custom agent in the registry."""
    return {
        "status": "ok",
        "core_agents": sorted(RESERVED_AGENT_IDS),
        "custom_agents": [spec.to_dict() for spec in load_specs()],
    }


# --------------------------------------------------------------------------- #
# Source inspection
# --------------------------------------------------------------------------- #
def list_project_files() -> dict[str, Any]:
    """List the application source files an agent is allowed to read."""
    files: list[str] = []
    for root in READABLE_SOURCE_ROOTS:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix.lower() in READABLE_SOURCE_SUFFIXES:
                if any(part.startswith(".") for part in path.parts):
                    continue
                files.append(str(path.relative_to(PROJECT_ROOT)))
    return {"status": "ok", "project_root": str(PROJECT_ROOT), "files": files}


def read_source_file(relative_path: str) -> dict[str, Any]:
    """Read one application source file listed by `list_project_files`.

    Only files under app/ and openwebui_functions/ with a source extension can
    be read. Do not use this to look for credentials — environment files and
    dotfiles are refused.
    """
    try:
        candidate = (PROJECT_ROOT / relative_path).resolve()
    except OSError as exc:
        return _error(exc)

    if not any(candidate.is_relative_to(root.resolve()) for root in READABLE_SOURCE_ROOTS):
        return {
            "status": "error",
            "error": "Only files under app/ and openwebui_functions/ can be read",
        }
    if any(part.startswith(".") for part in candidate.parts):
        return {"status": "error", "error": "Dotfiles are not readable"}
    if candidate.suffix.lower() not in READABLE_SOURCE_SUFFIXES:
        return {
            "status": "error",
            "error": f"{candidate.suffix!r} is not a readable source extension",
        }
    if not candidate.is_file():
        return {"status": "error", "error": f"No such file: {relative_path}"}
    if candidate.stat().st_size > MAX_SOURCE_READ_BYTES:
        return {"status": "error", "error": "File exceeds the 200 KB read limit"}

    return {
        "status": "ok",
        "path": str(candidate.relative_to(PROJECT_ROOT)),
        "content": candidate.read_text(encoding="utf-8", errors="replace"),
    }


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #
def database_status() -> dict[str, Any]:
    """Connectivity, extensions and knowledge tables for the Postgres backend."""
    try:
        from sqlalchemy import create_engine, text

        engine = create_engine(settings.database_url, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            extensions = [
                row[0] for row in conn.execute(text("SELECT extname FROM pg_extension ORDER BY 1"))
            ]
            tables = [
                f"{row[0]}.{row[1]}"
                for row in conn.execute(
                    text(
                        "SELECT schemaname, tablename FROM pg_tables "
                        "WHERE schemaname IN ('public', 'ai') ORDER BY 1, 2"
                    )
                )
            ]
        engine.dispose()
        return {
            "reachable": True,
            "extensions": extensions,
            "pgvector": "vector" in extensions,
            "tables": tables,
        }
    except Exception as exc:
        return {"reachable": False, "error": f"{type(exc).__name__}: {exc}"}


def inspect_platform_health() -> dict[str, Any]:
    """Full platform health: database, embeddings, tracing, watcher and agents."""
    return {
        "status": "ok",
        "agentos_id": settings.agentos_id,
        "database": database_status(),
        "llm": {
            "provider": "openrouter",
            "configured": settings.llm_configured,
            "default_model": settings.agno_default_model,
        },
        "embeddings": {
            "configured": km.embeddings_available(),
            "model": settings.embedding_model,
            "base_url": settings.embedding_base_url,
            "dimensions": settings.embedding_dimensions,
        },
        "telemetry": get_otel_status(),
        "watcher": watcher_service.status(),
        "workspace_folders": km.known_folders(),
        "agents": list_registered_agents(),
    }


WORKSPACE_TOOLS = [
    create_workspace_directory,
    list_workspace_directories_and_files,
    save_text_file_to_folder,
    route_openwebui_upload_to_folder,
    sync_folder_to_pgvector,
    inspect_file_watcher_status,
]

REGISTRY_TOOLS = [create_or_update_agent, delete_custom_agent, list_registered_agents]

SOURCE_TOOLS = [list_project_files, read_source_file]

HEALTH_TOOLS = [inspect_platform_health, inspect_file_watcher_status, list_registered_agents]


def project_file_path(relative_path: str) -> Path:
    """Helper used by tests to build paths relative to the project root."""
    return PROJECT_ROOT / relative_path
