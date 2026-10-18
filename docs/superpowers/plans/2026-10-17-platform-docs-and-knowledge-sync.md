# Platform Docs Agent & 3-Way Knowledge Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep Disk (`workspace_files/`), Postgres DB (`PgVector` + Agno `FileSystem` page storage), and Open WebUI (`Workspace → Knowledge` & User Folders) automatically in sync based on user and directory path, and add the `Platform Docs` (`platform-docs`) core specialist agent with structured document search, list, grep, read, and `llms.txt` / URL sync tools.

**Architecture:** `app/knowledge/paths.py` provides canonical folder/user path helpers (`user_folder_key`, `folder_to_namespace`). `app/knowledge/knowledge_manager.py` adds structured document inspection (`list_folder_documents`, `read_folder_document`, `grep_folder_documents`, `search_folder_knowledge`, `sync_external_docs`) and syncs text into Agno's Postgres `FileSystem` alongside `PgVector`. `app/bridge/openwebui_sync.py` reconciles Open WebUI's `knowledge`, `knowledge_directory`, `knowledge_file`, and `folder` tables with `workspace_files/` and attaches bound knowledge bases to `agent.<id>` Workspace Models. `app/agents/runtime.py` registers `platform-docs` (`Platform Docs`).

**Tech Stack:** Python 3.12+, Agno 3.1.4 (`Agent`, `Knowledge`, `PgVector`, `FileSystem`), `httpx`, `sqlite3` (read-only URI mode), `watchdog`, `pytest`.

## Global Constraints

- Never modify `/app/openwebui_data/webui.db` directly; open it strictly in read-only URI mode (`file:...?mode=ro`).
- All mutations in Open WebUI go through its HTTP API (`http://open-webui:8080/api/v1/...`).
- Document tools (`list_docs_pages`, `grep_docs_pages`, `read_doc_page`, `sync_external_docs_url`) and file staging must work even when `EMBEDDING_API_KEY` is not set.
- Run `graphify update .` after modifying code files.

---

### Task 1: User/Directory Path Helpers & Document Page Operations (`paths.py` & `knowledge_manager.py`)

**Files:**
- Modify: `app/knowledge/paths.py`
- Modify: `app/knowledge/knowledge_manager.py`
- Create: `tests/test_docs_tools.py`

**Interfaces:**
- Produces:
  - `paths.user_folder_key(user_identifier: str | None, folder: str | None) -> str`
  - `paths.folder_to_namespace(folder: str | None) -> str`
  - `knowledge_manager.list_folder_documents(folder: str | None = None) -> dict[str, Any]`
  - `knowledge_manager.read_folder_document(folder: str, filename: str, offset: int = 0, max_chars: int = 12000) -> dict[str, Any]`
  - `knowledge_manager.grep_folder_documents(query: str, folder: str | None = None, ignore_case: bool = True, limit: int = 30) -> dict[str, Any]`
  - `knowledge_manager.search_folder_knowledge(query: str, folder: str | None = None, limit: int = 8) -> dict[str, Any]`
  - `knowledge_manager.sync_external_docs(folder: str, url: str, reindex: bool = False, client: Any = None) -> dict[str, Any]`

- [ ] **Step 1: Write failing tests in `tests/test_docs_tools.py` for path helpers and document operations**
- [ ] **Step 2: Run `pytest tests/test_docs_tools.py` to confirm failure**
- [ ] **Step 3: Implement helpers in `app/knowledge/paths.py` and document/page operations in `app/knowledge/knowledge_manager.py`**
- [ ] **Step 4: Run `pytest tests/test_docs_tools.py` to confirm pass**

---

### Task 2: Platform Docs Tools & `Platform Docs` Core Specialist Agent (`platform_tools.py`, `store.py`, `runtime.py`, `settings.py`)

**Files:**
- Modify: `app/settings.py`
- Modify: `app/agents/store.py`
- Modify: `app/tools/platform_tools.py`
- Modify: `app/agents/runtime.py`
- Modify: `tests/test_docs_tools.py`

**Interfaces:**
- Consumes: `knowledge_manager` functions from Task 1
- Produces:
  - `settings.oap_docs_model: str`
  - `platform_tools.DOCS_TOOLS` (`search_docs_knowledge`, `list_docs_pages`, `grep_docs_pages`, `read_doc_page`, `sync_external_docs_url`, `sync_openwebui_knowledge`)
  - `"platform-docs"` in `store.PLATFORM_AGENT_IDS` and `runtime.CORE_AGENT_IDS`
  - `runtime._build_platform_docs() -> Agent`

- [ ] **Step 1: Add tests in `tests/test_docs_tools.py` for `DOCS_TOOLS` and `platform-docs` registration**
- [ ] **Step 2: Run `pytest tests/test_docs_tools.py` to confirm failure**
- [ ] **Step 3: Implement `DOCS_TOOLS` in `app/tools/platform_tools.py` and register `platform-docs` in `store.py`, `settings.py`, and `runtime.py`**
- [ ] **Step 4: Run `pytest tests/test_docs_tools.py` to confirm pass**

---

### Task 3: Automatic 3-Way Knowledge & Folder Sync (`openwebui_sync.py`, `file_watcher.py`, `openwebui_router.py`)

**Files:**
- Modify: `app/bridge/openwebui_sync.py`
- Modify: `app/knowledge/file_watcher.py`
- Modify: `app/bridge/openwebui_router.py`
- Modify: `tests/test_openwebui_sync.py`

**Interfaces:**
- Consumes: `paths.user_folder_key`, `km.stage_openwebui_upload`, `km.known_folders`, `km.list_folder_documents`
- Produces:
  - `openwebui_sync.sync_knowledge_with_openwebui(...) -> dict[str, Any]`
  - Automatic pull from Open WebUI `knowledge`, `knowledge_directory`, `knowledge_file`, and `folder` tables into `workspace_files/<path>/`
  - Automatic push from `workspace_files/<folder>/` into Open WebUI `/api/v1/knowledge/*` and `/api/v1/files/`
  - Automatic attachment of a custom agent/project's `knowledge_folder` to its `agent.<id>` Workspace Model (`meta.knowledge`)
  - Watcher `on_deleted` + background Open WebUI sync trigger on disk changes

- [ ] **Step 1: Add failing tests in `tests/test_openwebui_sync.py` for Open WebUI ↔ `workspace_files/` two-way knowledge & folder sync**
- [ ] **Step 2: Run `pytest tests/test_openwebui_sync.py` to confirm failure**
- [ ] **Step 3: Implement `sync_knowledge_with_openwebui` in `app/bridge/openwebui_sync.py` and wire into `file_watcher.py` and `openwebui_router.py`**
- [ ] **Step 4: Run the full test suite and `graphify update .`**
