# Platform Docs Agent & 3-Way Knowledge Sync — Design Spec

**Date:** 2026-10-17  
**Status:** Approved

---

## 1. Problem & Goal

In the current Open Agent Platform (OAP) architecture:
1. **Unidirectional file staging:** Files dropped into `workspace_files/<folder>/` or routed via `#folder:<path>` in chat are indexed into Postgres (`ai.kb_<slug>`), but Open WebUI's **Workspace → Knowledge** collections, sub-directories (`knowledge_directory`), and user **Chat Folders** (`folder`) are not reconciled with `workspace_files/`.
2. **UI-created folders and files stay stranded:** When a user creates a Knowledge collection, nested directory, or chat folder in Open WebUI and attaches files to it, those files sit in `/app/openwebui_data/uploads` without automatically syncing into `workspace_files/<path>/` or the Postgres knowledge tables.
3. **Chunk-only retrieval without structured document navigation:** Agents bound to a knowledge folder only get top-k vector chunks (`search_knowledge=True`). They lack page-storage capabilities (listing documents, grepping literal/regex patterns across a folder, reading full document sections with offsets, or syncing external `llms.txt` / Markdown documentation sites).

**Goal:**
1. Keep **Disk (`workspace_files/`) ↔ Postgres DB (`PgVector` + `FileSystem`/`page_store`) ↔ Open WebUI (`Workspace → Knowledge` & User Folders)** automatically in sync based on **user and directory path**, for any file, document, or reference knowledge folder.
2. Add a core **`Platform Docs`** specialist agent (`id="platform-docs"`, synced to Open WebUI as `agent.platform-docs`) and reusable document/page tools (`DOCS_TOOLS`) so **any** knowledge folder — contracts, SOPs, project notes, user uploads, product manuals, vendor PDFs, or an external `llms.txt` / Markdown site — can be searched, grepped, read in full, and synchronized automatically.

Agno's page-storage API is only the **publish mechanism** (text + catalog + vectors in one transaction). It is **not** the content source. Nothing in this design is limited to `docs.agno.com` or Agno documentation.

---

## 2. Directory & User Path Conventions

Every knowledge folder has a canonical relative POSIX key under `workspace_files/` (validated and normalized by `app/knowledge/paths.py`):

| Scope | `workspace_files/` Path | Postgres Vector Table | Postgres `FileSystem` / Page Namespace | Open WebUI Mapping |
|---|---|---|---|---|
| **Shared Topic / Reference Folder** | `workspace_files/<folder>/` (e.g. `general`, `contracts`, `sops`, `docs/api`) | `ai.kb_<slug>` | `kb-<slug>` | Knowledge Base named `<folder>` (nested paths map to `knowledge_directory` tree inside top-level Knowledge Base) |
| **User-Scoped Folder** | `workspace_files/users/<user_slug>/<folder>/` | `ai.kb_users_<user>_<folder>` | `kb-users-<user>-<folder>` | User-owned Chat Folder (`folder` table) or personal Knowledge Base in Open WebUI |
| **Project Folder** | `workspace_files/projects/<project_id>/` | `ai.kb_projects_<id>` | `kb-projects-<id>` | Knowledge Base `project:<project_id>` attached to Workspace Model `agent.<project_id>` |
| **Custom Agent Folder** | `workspace_files/agents/<agent_id>/` | `ai.kb_agents_<id>` | `kb-agents-<id>` | Knowledge Base `agent:<agent_id>` attached to Workspace Model `agent.<agent_id>` |

### Path Resolution Rules (`app/knowledge/paths.py`)
- `user_folder_key(user_id_or_name: str, folder: str) -> str`: Normalizes a user identifier (`email` prefix, `name`, or `id`) and folder name into `users/<user_slug>/<folder_slug>`.
- If a folder key already starts with `users/`, `projects/`, or `agents/`, it is preserved as-is.
- Every path passes through `normalise_folder()` and `resolve_within(WORKSPACE_ROOT, ...)` so traversal (`..`, `~`, symlinks) is impossible.

---

## 3. Architecture & Component Boundaries

```text
  ┌─────────────────────────┐        ┌──────────────────────────────────────────┐
  │   workspace_files/      │        │               oap-db (Postgres)          │
  │  ├─ general/            │◀──────▶│  ├─ ai.kb_<slug> (PgVector hybrid)       │
  │  ├─ contracts/          │        │  ├─ ai.agno_fs   (FileSystem page store) │
  │  ├─ projects/<id>/      │        │  └─ ai.agno_knowledge (content catalog)  │
  │  ├─ agents/<id>/        │        └────────────────────▲─────────────────────┘
  │  └─ users/<user>/<dir>/ │                             │
  └────────────▲────────────┘                             │
               │                                          │
      watchdog │ debounced                       index /  │ read / grep /
       events  │ + SHA-256                       publish  │ sync_pages
               ▼                                          ▼
  ┌─────────────────────────────────────────────────────────────────────────────┐
  │                     app/knowledge/knowledge_manager.py                      │
  │                     app/bridge/openwebui_sync.py                            │
  └────────────────────────────────────▲────────────────────────────────────────┘
                                       │
                     read-only SQLite  │  + Open WebUI REST API
                     (`webui.db`)      │  (`/api/v1/knowledge/*`, `/api/v1/models/*`)
                                       ▼
  ┌─────────────────────────────────────────────────────────────────────────────┐
  │                         Open WebUI (http://open-webui:8080)                 │
  │  ├─ Workspace → Knowledge (`knowledge`, `knowledge_directory`, `file`)      │
  │  ├─ Sidebar Chat Folders  (`folder` + user uploads)                         │
  │  └─ Workspace → Models    (`agent.<id>` with `meta.knowledge` attached)     │
  └─────────────────────────────────────────────────────────────────────────────┘
```

### 3.1 Postgres Page & Document Store (`app/knowledge/knowledge_manager.py`)
Enhance `knowledge_manager.py` so every folder has both vector search and structured document/page storage:
1. **Per-Folder `FileSystem` Namespace (`get_or_create_folder_fs(folder)`):**
   - Backed by Agno's `FileSystem(db=_get_content_db(), namespace=folder_to_namespace(folder))`.
   - Whenever `index_single_file(path)` or `save_text_file(folder, filename, content)` runs on a readable text/markdown/CSV/JSON/PDF/DOCX file, the extracted text/markdown is also written to the folder's `FileSystem` namespace (`/<filename>`) alongside the PgVector hybrid chunks.
   - When `page_store` is supported (for `.md` / `llms.txt` syncs via `agno[pages]`), `Knowledge` is configured with `page_store=get_or_create_folder_fs(folder)` so `sync_pages(url=...)` publishes pages atomically.
2. **Structured Document Inspection & Search Functions:**
   - `list_folder_documents(folder: str | None = None) -> dict[str, Any]`: Lists files on disk in `workspace_files/<folder>/` (or all folders when `folder=None`) with byte size, SHA-256 digest, modified timestamp, and PgVector table name.
   - `read_folder_document(folder: str, filename: str, offset: int = 0, max_chars: int = 12000) -> dict[str, Any]`: Reads text from a document in `workspace_files/<folder>/<filename>` (including `.md`, `.txt`, `.json`, `.csv`, and extracted text from `.pdf` / `.docx`) with character pagination (`offset`, `max_chars`, `total_chars`, `has_more`).
   - `grep_folder_documents(query: str, folder: str | None = None, ignore_case: bool = True, limit: int = 30) -> dict[str, Any]`: Searches literal or regex matches across files in `workspace_files/<folder>` (or all folders) and returns matching file paths, line numbers, and snippets.
   - `search_folder_knowledge(query: str, folder: str | None = None, limit: int = 8) -> dict[str, Any]`: Runs hybrid search against `get_or_create_folder_knowledge(folder)` (or across known folders) and returns ranked chunks with source file and folder metadata.
   - `sync_external_docs(folder: str, url: str, reindex: bool = False) -> dict[str, Any]`:
     - Works for **any** documentation site or file URL, not Agno docs. Examples: a vendor `llms.txt`, a company handbook Markdown tree, a single runbook `.md` / `.txt`.
     - If `url` points to an `llms.txt` index (or a domain with `/llms.txt`), fetches discovered Markdown pages (using `Knowledge.sync_pages` when `agno[pages]` is active, or an HTTP fetcher that writes each page into `workspace_files/<folder>/` and indexes it into PgVector + `FileSystem`).
     - If `url` points to a single `.md` / `.txt` / documentation URL, fetches and saves it into `workspace_files/<folder>/` and indexes it.
     - Local files already in that folder (`.pdf`, `.docx`, `.csv`, `.json`, `.txt`, `.md`) stay the source of truth and are indexed the same way. External sync is an extra ingest path, not a replacement.

### 3.2 Two-Way Open WebUI Knowledge & Folder Reconciler (`app/bridge/openwebui_sync.py`)
Extend `openwebui_sync.py` with `sync_knowledge_with_openwebui()`:
1. **Pull from Open WebUI (`webui.db` → `workspace_files/` + Postgres):**
   - Queries `/app/openwebui_data/webui.db` (read-only) for:
     - **Knowledge Bases & Directories:** `knowledge`, `knowledge_directory`, `knowledge_file`, and `file` joined with `"user"` (to resolve user slug).
       - A Knowledge Base named `contracts` (or tagged `OAP`) maps to `workspace_files/contracts/`.
       - Nested `knowledge_directory` rows (`parent_id` chain) map to `workspace_files/<kb_slug>/<dir_slug>/...`.
       - A user-created Knowledge Base owned by a non-admin user maps to `workspace_files/users/<user_slug>/<kb_slug>/` (or `workspace_files/<kb_slug>/` when shared publicly).
     - **User Chat Folders (`folder` table):**
       - Chat folders in Open WebUI that have uploaded files in `items` / `data` or chats with attached files map to `workspace_files/users/<user_slug>/<folder_path>/`.
   - Any attached `file` in `webui.db` whose stored file exists in `/app/openwebui_data/uploads` is staged idempotently into the resolved `workspace_files/<path>/` via `km.stage_openwebui_upload(..., auto_index=True)`.
2. **Push from OAP (`workspace_files/` → Open WebUI Knowledge UI):**
   - Lists all non-empty folders in `km.known_folders()`.
   - Ensures each top-level folder has a corresponding Open WebUI Knowledge Base via `/api/v1/knowledge/create` (with `PUBLIC_READ_GRANT` so users can see and attach it in Open WebUI), and nested subfolders have matching `/api/v1/knowledge/{id}/dirs/create` directories.
   - Uploads any local files from `workspace_files/<folder>/` that are not yet registered in that Open WebUI Knowledge Base via `POST /api/v1/files/` + `POST /api/v1/knowledge/{id}/file/add`.
   - Attaches the synced Knowledge Base metadata (`meta.knowledge`) to any OAP Workspace Model (`agent.<id>`) that has a bound `knowledge_folder`.
3. **Loop Prevention & Idempotency:**
   - Tracks `(folder, filename, sha256_digest)` in memory and checks file content hashes before copying or uploading.
   - When the file watcher detects a change on disk (`file_watcher.py`), after indexing the file into PgVector it triggers `openwebui_sync.ensure_synced_in_background()` to reconcile Open WebUI asynchronously.

### 3.3 Core Specialist Agent: `Platform Docs` (`id="platform-docs"`)
Add `platform-docs` (**Platform Docs**) to `CORE_AGENT_IDS` in `app/agents/runtime.py` and `RESERVED_AGENT_IDS` in `app/agents/store.py`:
- **Display Name:** `Platform Docs`
- **Role:** `Manages, searches, reads and syncs documentation and reference knowledge folders across disk, Postgres and Open WebUI`
- **Model:** `build_model(settings.oap_default_model)`
- **Tools (`DOCS_TOOLS` + `WORKSPACE_TOOLS`):**
  - `search_docs_knowledge(query, folder=None)`
  - `list_docs_pages(folder=None)`
  - `grep_docs_pages(query, folder=None, ignore_case=True)`
  - `read_doc_page(folder, filename, offset=0, max_chars=12000)`
  - `sync_external_docs_url(folder, url, reindex=False)`
  - `sync_openwebui_knowledge()`
  - Plus `WORKSPACE_TOOLS` (`create_workspace_directory`, `list_workspace_directories_and_files`, `save_text_file_to_folder`, `route_openwebui_upload_to_folder`, `sync_folder_to_pgvector`, `inspect_file_watcher_status`).
- **Coordinator (`oap`) Routing Update:**
  - Add instruction to `Platform Coordinator`: `"Documentation lookup, reference folders, grepping/reading docs, and syncing external or Open WebUI knowledge goes to Platform Docs."`
- **Project Specialists (`project-researcher` & `project-dev`):**
  - Also receive the read/search/grep document tools (`DOCS_READ_TOOLS`) so any Project or custom agent can inspect full files in its bound knowledge folder.

---

## 4. Error Handling & Edge Cases

- **Missing Embeddings Key:**
  - Full-text `list_docs_pages`, `grep_docs_pages`, `read_doc_page`, `sync_external_docs_url` (file download), and Open WebUI ↔ `workspace_files/` file syncing all work **even when `EMBEDDING_API_KEY` is not set**. Vector indexing (`PgVector`) is skipped cleanly with a clear status note, and `grep_docs_pages` / `read_doc_page` still give agents complete access to the files.
- **Deleted Files on Disk:**
  - Add `on_deleted` handling to `DebouncedIndexHandler` in `app/knowledge/file_watcher.py` so deleting a file from `workspace_files/<folder>/` logs the deletion event and triggers background reconciliation.
- **Unreachable Open WebUI or Fresh Install:**
  - Like `sync_models_to_openwebui()`, `sync_knowledge_with_openwebui()` returns `{"status": "skipped", "reason": "..."}` without raising if `webui.db` has no admin session yet or Open WebUI is offline.

---

## 5. Testing Strategy

1. **`tests/test_docs_tools.py`:**
   - Unit tests for `list_folder_documents`, `read_folder_document` (including pagination offsets), `grep_folder_documents` (literal and regex matches across folders), `user_folder_key` path normalization, and `sync_external_docs` (mocked HTTP `llms.txt` and single Markdown page download into `workspace_files/<folder>/`).
2. **`tests/test_openwebui_sync.py`:**
   - Tests for `sync_knowledge_with_openwebui()`:
     - Pulling files from Open WebUI `knowledge`, `knowledge_directory`, `knowledge_file`, and `folder` tables in SQLite `webui.db` into `workspace_files/<folder>/` and `workspace_files/users/<user>/<folder>/`.
     - Pushing local `workspace_files/<folder>/` folders and files to Open WebUI's `/api/v1/knowledge/*` endpoints and deduplicating unchanged files by SHA-256 digest.
3. **`tests/test_bridge.py` & `tests/test_agent_store.py`:**
   - Verify `platform-docs` is included in `CORE_AGENT_IDS` / `RESERVED_AGENT_IDS` and advertised by `describe_agents()`.
