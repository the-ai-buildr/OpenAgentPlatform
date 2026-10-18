"""Per-folder PgVector knowledge bases.

Each directory under `workspace_files/` maps to one Agno `Knowledge` instance
backed by its own PgVector table (`kb_<slug>`), so an agent bound to a folder
only ever retrieves documents from that folder.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from app.knowledge.paths import (
    DEFAULT_FOLDER,
    SUPPORTED_EXTENSIONS,
    UnsafePathError,
    folder_dir,
    folder_to_namespace,
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
tracer = get_tracer("oap.knowledge")

_CACHE: dict[str, Any] = {}
_FS_CACHE: dict[str, Any] = {}
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

        _content_db = PostgresDb(id="oap-knowledge", db_url=settings.database_url)
    return _content_db


def build_embedder() -> Any:
    """Embedder for every knowledge base.

    The base URL defaults to the OpenAI API. OpenRouter also serves
    `/embeddings`; set `EMBEDDING_BASE_URL` to it and use an OpenRouter model
    id such as `openai/text-embedding-3-small`.
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


def get_or_create_folder_fs(folder: str | None) -> Any | None:
    """Return (and cache) the Agno `FileSystem` page store for `folder`."""
    key = normalise_folder(folder)
    with _CACHE_LOCK:
        if key in _FS_CACHE:
            return _FS_CACHE[key]
        try:
            from agno.fs import FileSystem

            fs = FileSystem(
                db=_get_content_db(),
                namespace=folder_to_namespace(key),
                allow_delete=True,
            )
            _FS_CACHE[key] = fs
            return fs
        except Exception as exc:
            logger.debug("FileSystem page store for %r unavailable: %s", key, exc)
            return None


def _extract_file_text(path: Path) -> str:
    """Read text from a workspace file (including .docx and .pdf when possible)."""
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md", ".csv", ".json"}:
        return path.read_text(encoding="utf-8", errors="replace")
    if suffix == ".docx":
        try:
            import docx  # type: ignore[import-not-found]

            doc = docx.Document(str(path))
            return "\n".join(p.text for p in doc.paragraphs if p.text)
        except Exception:
            return path.read_text(encoding="utf-8", errors="replace")
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader  # type: ignore[import-not-found]

            reader = PdfReader(str(path))
            return "\n\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception:
            return path.read_text(encoding="utf-8", errors="replace")
    return path.read_text(encoding="utf-8", errors="replace")


def _sync_file_to_fs(folder: str, path: Path) -> bool:
    """Mirror a workspace file's text into the Postgres FileSystem namespace (best-effort)."""
    try:
        fs = get_or_create_folder_fs(folder)
        if fs is None:
            return False
        text = _extract_file_text(path)
        fs.write(f"/{path.name}", text, overwrite=True)
        return True
    except Exception as exc:
        logger.debug("Could not mirror %s to FileSystem: %s", path.name, exc)
        return False


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
        fs = get_or_create_folder_fs(key)
        knowledge = Knowledge(
            name=table,
            description=f"Documents stored in workspace_files/{key}",
            content_db=_get_content_db(),
            page_store=fs,
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
    """Knowledge bases to hand to the platform; empty when embeddings are off."""
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
    norm_folder = normalise_folder(folder)
    result: dict[str, Any] = {
        "saved_to": str(destination),
        "folder": norm_folder,
        "bytes": len(content.encode("utf-8")),
        "indexed": False,
    }
    if auto_index and is_supported_file(destination):
        _sync_file_to_fs(norm_folder, destination)
        if embeddings_available():
            try:
                index_single_file(destination, recreate=True)
                result["indexed"] = True
            except Exception as exc:
                result["index_error"] = str(exc)
    return result


def list_folder_documents(folder: str | None = None) -> dict[str, Any]:
    """List supported documents in one workspace folder (or across all folders when `folder=None`)."""
    target_folders = [normalise_folder(folder)] if (folder and folder.strip()) else known_folders()
    documents: list[dict[str, Any]] = []
    for key in target_folders:
        directory = folder_dir(WORKSPACE_ROOT, key)
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if not path.is_file() or path.name.startswith("."):
                continue
            stat = path.stat()
            documents.append(
                {
                    "folder": key,
                    "filename": path.name,
                    "bytes": stat.st_size,
                    "sha256": _digest(path),
                    "supported": is_supported_file(path),
                    "pgvector_table": folder_to_table_name(key),
                    "namespace": folder_to_namespace(key),
                }
            )
    return {
        "folder": normalise_folder(folder) if (folder and folder.strip()) else None,
        "folders": target_folders,
        "total_documents": len(documents),
        "documents": documents,
    }


def read_folder_document(
    folder: str, filename: str, offset: int = 0, max_chars: int = 12000
) -> dict[str, Any]:
    """Read a document from `workspace_files/<folder>/<filename>` with pagination."""
    key = normalise_folder(folder)
    directory = folder_dir(WORKSPACE_ROOT, key)
    safe_name = safe_filename(filename)
    candidate = resolve_within(directory, safe_name)
    if not candidate.is_file():
        # Try case-insensitive match within the folder
        matches = [
            p
            for p in (directory.iterdir() if directory.is_dir() else [])
            if p.is_file() and p.name.lower() == filename.strip().lower()
        ]
        if not matches:
            raise FileNotFoundError(f"Document {filename!r} not found in folder {key!r}")
        candidate = matches[0]

    text = _extract_file_text(candidate)
    start = max(0, int(offset))
    limit = max(1, min(int(max_chars), 50000))
    chunk = text[start : start + limit]
    end = start + len(chunk)
    return {
        "folder": key,
        "filename": candidate.name,
        "offset": start,
        "max_chars": limit,
        "total_chars": len(text),
        "has_more": end < len(text),
        "next_offset": end if end < len(text) else None,
        "content": chunk,
    }


def grep_folder_documents(
    query: str,
    folder: str | None = None,
    ignore_case: bool = True,
    limit: int = 30,
) -> dict[str, Any]:
    """Search literal or regex matches across documents in `folder` (or all folders)."""
    needle = (query or "").strip()
    if not needle:
        raise ValueError("query is required")
    flags = re.IGNORECASE if ignore_case else 0
    try:
        pattern = re.compile(needle, flags)
    except re.error:
        pattern = re.compile(re.escape(needle), flags)

    target_folders = [normalise_folder(folder)] if (folder and folder.strip()) else known_folders()
    matches: list[dict[str, Any]] = []
    max_hits = max(1, min(int(limit), 200))
    for key in target_folders:
        directory = folder_dir(WORKSPACE_ROOT, key)
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if not path.is_file() or not is_supported_file(path):
                continue
            try:
                text = _extract_file_text(path)
            except Exception:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line):
                    matches.append(
                        {
                            "folder": key,
                            "filename": path.name,
                            "line": lineno,
                            "snippet": line.strip()[:300],
                        }
                    )
                    if len(matches) >= max_hits:
                        return {
                            "query": needle,
                            "folder": normalise_folder(folder) if (folder and folder.strip()) else None,
                            "total_matches": len(matches),
                            "truncated": True,
                            "matches": matches,
                        }
    return {
        "query": needle,
        "folder": normalise_folder(folder) if (folder and folder.strip()) else None,
        "total_matches": len(matches),
        "truncated": False,
        "matches": matches,
    }


def search_folder_knowledge(
    query: str, folder: str | None = None, limit: int = 8
) -> dict[str, Any]:
    """Hybrid search across a folder's PgVector knowledge base, falling back to grep when embeddings are off."""
    needle = (query or "").strip()
    if not needle:
        raise ValueError("query is required")
    if not embeddings_available():
        fallback = grep_folder_documents(needle, folder=folder, limit=limit)
        return {
            "mode": "grep_fallback",
            "reason": "EMBEDDING_API_KEY is not set; used literal/regex grep fallback",
            **fallback,
        }

    target_folders = [normalise_folder(folder)] if (folder and folder.strip()) else known_folders()
    results: list[dict[str, Any]] = []
    for key in target_folders:
        try:
            kb = get_or_create_folder_knowledge(key)
            docs = kb.search(query=needle, max_results=limit)
            for doc in docs or []:
                results.append(
                    {
                        "folder": key,
                        "name": getattr(doc, "name", "") or "",
                        "content": getattr(doc, "content", "") or "",
                        "meta_data": getattr(doc, "meta_data", {}) or {},
                    }
                )
        except Exception as exc:
            logger.debug("Vector search failed for folder %s: %s", key, exc)
    if not results:
        fallback = grep_folder_documents(needle, folder=folder, limit=limit)
        return {"mode": "hybrid_plus_grep", "results": [], **fallback}
    return {
        "mode": "hybrid_vector",
        "query": needle,
        "folder": normalise_folder(folder) if (folder and folder.strip()) else None,
        "total_results": len(results[:limit]),
        "results": results[:limit],
    }


_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s\)]+)\)")


def sync_external_docs(
    folder: str,
    url: str,
    reindex: bool = False,
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Fetch an `llms.txt` index or Markdown documentation URL into `workspace_files/<folder>/` and index it."""
    key = normalise_folder(folder)
    target_url = (url or "").strip()
    if not target_url.startswith(("http://", "https://")):
        raise ValueError("url must start with http:// or https://")

    owns_client = client is None
    http_client = client or httpx.Client(timeout=20.0, follow_redirects=True)
    synced_files: list[str] = []
    errors: list[str] = []
    try:
        resp = http_client.get(target_url)
        resp.raise_for_status()
        body = resp.text

        parsed = urlparse(target_url)
        raw_name = Path(parsed.path).name or "index.md"
        if not Path(raw_name).suffix:
            raw_name = f"{raw_name}.md"
        save_res = save_text_file(key, raw_name, body, auto_index=True)
        synced_files.append(Path(save_res["saved_to"]).name)

        # If this is an llms.txt index, discover linked Markdown/text pages and sync them too.
        if Path(parsed.path).name.lower() in {"llms.txt", "llms-full.txt"}:
            discovered_urls: list[str] = []
            for _title, link_url in _MD_LINK_RE.findall(body):
                link_path = urlparse(link_url).path.lower()
                if link_path.endswith((".md", ".txt", ".mdx")) and link_url not in discovered_urls:
                    discovered_urls.append(link_url)
            for page_url in discovered_urls[:50]:
                try:
                    page_resp = http_client.get(page_url)
                    page_resp.raise_for_status()
                    page_name = Path(urlparse(page_url).path).name or "page.md"
                    if page_name.lower().endswith(".mdx"):
                        page_name = page_name[:-4] + ".md"
                    saved = save_text_file(key, page_name, page_resp.text, auto_index=True)
                    fname = Path(saved["saved_to"]).name
                    if fname not in synced_files:
                        synced_files.append(fname)
                except Exception as exc:
                    errors.append(f"{page_url}: {exc}")

        if reindex and embeddings_available():
            sync_folder(key, recreate=True)

        return {
            "status": "ok",
            "folder": key,
            "source_url": target_url,
            "synced_files": synced_files,
            "errors": errors,
        }
    finally:
        if owns_client:
            http_client.close()


def reset_cache() -> None:
    """Drop cached Knowledge objects (used by tests and after config changes)."""
    with _CACHE_LOCK:
        _CACHE.clear()
        _FS_CACHE.clear()


__all__ = [
    "KnowledgeUnavailableError",
    "UnsafePathError",
    "build_embedder",
    "embeddings_available",
    "ensure_folder",
    "find_openwebui_upload",
    "get_all_knowledge_list",
    "get_or_create_folder_fs",
    "get_or_create_folder_knowledge",
    "grep_folder_documents",
    "index_single_file",
    "known_folders",
    "list_folder_documents",
    "list_openwebui_uploads",
    "read_folder_document",
    "reset_cache",
    "save_text_file",
    "search_folder_knowledge",
    "stage_openwebui_upload",
    "sync_external_docs",
    "sync_folder",
]
