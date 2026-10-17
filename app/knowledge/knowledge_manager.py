"""Per-folder PgVector knowledge bases.

Each directory under `workspace_files/` maps to one Agno `Knowledge` instance
backed by its own PgVector table (`kb_<slug>`), so an agent bound to a folder
only ever retrieves documents from that folder.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import threading
from pathlib import Path
from typing import Any

from app.knowledge.paths import (
    DEFAULT_FOLDER,
    SUPPORTED_EXTENSIONS,
    UnsafePathError,
    folder_dir,
    folder_to_table_name,
    is_supported_file,
    normalise_folder,
    relative_folder_for,
    resolve_within,
    safe_filename,
    strip_openwebui_prefix,
)
from app.settings import OPENWEBUI_UPLOADS_DIR, WORKSPACE_ROOT, settings
from app.telemetry.otel import get_tracer

logger = logging.getLogger(__name__)
tracer = get_tracer("agno.knowledge")

_CACHE: dict[str, Any] = {}
_CACHE_LOCK = threading.RLock()
_content_db: Any = None


class KnowledgeUnavailableError(RuntimeError):
    """Raised when indexing is requested but no embedding credential is set."""


def embeddings_available() -> bool:
    return settings.embeddings_configured


def _get_content_db() -> Any:
    global _content_db
    if _content_db is None:
        from agno.db.postgres import PostgresDb

        _content_db = PostgresDb(id="agentos-knowledge", db_url=settings.database_url)
    return _content_db


def build_embedder() -> Any:
    """Embedder for every knowledge base.

    Note the base URL defaults to the OpenAI API rather than OpenRouter:
    OpenRouter's catalogue contains no embedding models and it exposes no
    `/embeddings` route, so routing embeddings through it always fails.
    """
    from agno.knowledge.embedder.openai import OpenAIEmbedder

    if not settings.effective_embedding_api_key:
        raise KnowledgeUnavailableError(
            "EMBEDDING_API_KEY is not set. Knowledge indexing and retrieval are disabled; "
            "set an OpenAI-compatible embeddings key to enable them."
        )
    return OpenAIEmbedder(
        id=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
        api_key=settings.effective_embedding_api_key,
        base_url=settings.embedding_base_url,
    )


def ensure_folder(folder: str | None) -> Path:
    directory = folder_dir(WORKSPACE_ROOT, folder)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def get_or_create_folder_knowledge(folder: str | None) -> Any:
    """Return (and cache) the `Knowledge` instance owning `folder`."""
    key = normalise_folder(folder)
    with _CACHE_LOCK:
        if key in _CACHE:
            return _CACHE[key]

        from agno.knowledge.knowledge import Knowledge
        from agno.vectordb.pgvector import PgVector, SearchType

        ensure_folder(key)
        table = folder_to_table_name(key)
        knowledge = Knowledge(
            name=table,
            description=f"Documents stored in workspace_files/{key}",
            content_db=_get_content_db(),
            vector_db=PgVector(
                table_name=table,
                db_url=settings.database_url,
                search_type=SearchType.hybrid,
                embedder=build_embedder(),
            ),
        )
        _CACHE[key] = knowledge
        return knowledge


def known_folders() -> list[str]:
    """Folder keys that exist on disk, always including `general`."""
    ensure_folder(DEFAULT_FOLDER)
    folders = {DEFAULT_FOLDER}
    root = WORKSPACE_ROOT.resolve()
    for path in root.rglob("*"):
        if path.is_dir() and not any(part.startswith(".") for part in path.parts):
            folders.add(path.relative_to(root).as_posix())
    return sorted(folders)


def get_all_knowledge_list() -> list[Any]:
    """Knowledge bases to hand to AgentOS; empty when embeddings are off."""
    if not embeddings_available():
        logger.warning("No embedding credential configured - knowledge bases are not loaded")
        return []
    return [get_or_create_folder_knowledge(folder) for folder in known_folders()]


def _content_name(folder: str, path: Path) -> str:
    return f"{folder}/{path.name}"


def index_single_file(path: str | Path, recreate: bool = False) -> dict[str, Any]:
    """Index one file into the knowledge base that owns its folder."""
    resolved = resolve_within(WORKSPACE_ROOT, path)
    if not resolved.is_file():
        raise FileNotFoundError(f"No such file under the workspace: {path}")
    if not is_supported_file(resolved):
        return {
            "status": "skipped",
            "reason": f"unsupported extension {resolved.suffix!r}",
            "file": resolved.name,
        }
    if not embeddings_available():
        raise KnowledgeUnavailableError(
            "EMBEDDING_API_KEY is not set, so files are stored but not indexed."
        )

    folder = relative_folder_for(WORKSPACE_ROOT, resolved)
    with tracer.start_as_current_span("pgvector.index_single_file") as span:
        span.set_attribute("kb.folder", folder)
        span.set_attribute("kb.table", folder_to_table_name(folder))
        knowledge = get_or_create_folder_knowledge(folder)
        knowledge.insert(
            name=_content_name(folder, resolved),
            path=str(resolved),
            skip_if_exists=not recreate,
            upsert=True,
        )
    return {
        "status": "indexed",
        "file": resolved.name,
        "folder": folder,
        "table": folder_to_table_name(folder),
    }


def sync_folder(folder: str | None, recreate: bool = False) -> dict[str, Any]:
    """Index every supported file in a folder (non-recursive subdirectories get
    their own knowledge base, so this walks only `folder` itself)."""
    key = normalise_folder(folder)
    directory = ensure_folder(key)
    files = sorted(p for p in directory.iterdir() if p.is_file() and is_supported_file(p))
    result: dict[str, Any] = {
        "folder": key,
        "table": folder_to_table_name(key),
        "files_found": len(files),
        "indexed_files": [],
        "errors": [],
    }
    if not embeddings_available():
        result["errors"].append("EMBEDDING_API_KEY is not set; nothing was indexed")
        return result

    with tracer.start_as_current_span("pgvector.sync_folder") as span:
        span.set_attribute("kb.folder", key)
        span.set_attribute("kb.files_found", len(files))
        knowledge = get_or_create_folder_knowledge(key)
        if recreate:
            try:
                knowledge.vector_db.drop()
            except Exception as exc:  # pragma: no cover - depends on DB state
                result["errors"].append(f"drop failed: {exc}")
        for path in files:
            try:
                knowledge.insert(
                    name=_content_name(key, path),
                    path=str(path),
                    skip_if_exists=not recreate,
                    upsert=True,
                )
                result["indexed_files"].append(path.name)
            except Exception as exc:
                result["errors"].append(f"{path.name}: {exc}")
    return result


def find_openwebui_upload(filename_query: str) -> Path | None:
    """Newest Open WebUI upload whose stored name contains `filename_query`."""
    if not OPENWEBUI_UPLOADS_DIR.is_dir():
        return None
    needle = filename_query.strip().lower()
    candidates = [
        path
        for path in OPENWEBUI_UPLOADS_DIR.iterdir()
        if path.is_file() and (not needle or needle in path.name.lower())
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def list_openwebui_uploads(limit: int = 15) -> list[dict[str, Any]]:
    if not OPENWEBUI_UPLOADS_DIR.is_dir():
        return []
    files = sorted(
        (p for p in OPENWEBUI_UPLOADS_DIR.iterdir() if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return [
        {
            "stored_name": p.name,
            "display_name": strip_openwebui_prefix(p.name),
            "bytes": p.stat().st_size,
        }
        for p in files[:limit]
    ]


def _digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def stage_openwebui_upload(
    filename_query: str,
    target_folder: str | None = None,
    auto_index: bool = True,
) -> dict[str, Any]:
    """Copy an Open WebUI upload into the workspace and index it.

    Staging is idempotent: if the destination already holds a byte-identical
    file the copy is skipped. That makes it safe for the Filter, the Pipe and
    the chat bridge to all attempt routing for the same message.
    """
    with tracer.start_as_current_span("openwebui.stage_upload") as span:
        source = find_openwebui_upload(filename_query)
        if source is None:
            raise FileNotFoundError(
                f"No Open WebUI upload matching {filename_query!r}. Is the "
                "openwebui_data volume mounted into the API container?"
            )
        if source.stat().st_size > settings.max_staged_upload_bytes:
            raise ValueError(
                f"{source.name} is larger than MAX_STAGED_UPLOAD_BYTES "
                f"({settings.max_staged_upload_bytes} bytes)"
            )

        folder = normalise_folder(target_folder)
        directory = ensure_folder(folder)
        destination = directory / safe_filename(strip_openwebui_prefix(source.name))
        span.set_attribute("kb.folder", folder)
        span.set_attribute("file.name", destination.name)

        already_present = destination.exists() and _digest(destination) == _digest(source)
        if not already_present:
            shutil.copy2(source, destination)

        result: dict[str, Any] = {
            "source": str(source),
            "folder": folder,
            "saved_to": str(destination),
            "deduplicated": already_present,
            "pgvector_table": folder_to_table_name(folder),
            "indexed": False,
        }
        if auto_index and is_supported_file(destination):
            if not embeddings_available():
                result["index_error"] = "EMBEDDING_API_KEY is not set; file stored but not indexed"
            else:
                try:
                    index_single_file(destination, recreate=not already_present)
                    result["indexed"] = True
                except Exception as exc:
                    result["index_error"] = str(exc)
        elif auto_index:
            result["index_error"] = (
                f"{destination.suffix!r} is not one of {sorted(SUPPORTED_EXTENSIONS)}"
            )
        return result


def save_text_file(
    folder: str | None, filename: str, content: str, auto_index: bool = True
) -> dict[str, Any]:
    directory = ensure_folder(folder)
    destination = resolve_within(directory, safe_filename(filename))
    destination.write_text(content, encoding="utf-8")
    result: dict[str, Any] = {
        "saved_to": str(destination),
        "folder": normalise_folder(folder),
        "bytes": len(content.encode("utf-8")),
        "indexed": False,
    }
    if auto_index and is_supported_file(destination) and embeddings_available():
        try:
            index_single_file(destination, recreate=True)
            result["indexed"] = True
        except Exception as exc:
            result["index_error"] = str(exc)
    return result


def reset_cache() -> None:
    """Drop cached Knowledge objects (used by tests and after config changes)."""
    with _CACHE_LOCK:
        _CACHE.clear()


__all__ = [
    "KnowledgeUnavailableError",
    "UnsafePathError",
    "build_embedder",
    "embeddings_available",
    "ensure_folder",
    "find_openwebui_upload",
    "get_all_knowledge_list",
    "get_or_create_folder_knowledge",
    "index_single_file",
    "known_folders",
    "list_openwebui_uploads",
    "reset_cache",
    "save_text_file",
    "stage_openwebui_upload",
    "sync_folder",
]
