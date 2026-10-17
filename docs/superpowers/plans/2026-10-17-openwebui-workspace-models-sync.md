# Open WebUI Workspace Models Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically synchronize OAP's coordinator team, core agents, catalog entries, and custom builder agents into Open WebUI's **Workspace → Models** (`model` table with `base_model_id` set and hidden base models), keep them in sync when agents are created or deleted at runtime, and expose a sync/inspection tool to **Platform Builder** and **Platform Manager**.

**Architecture:** `app/bridge/openwebui_sync.py` mints a short-lived HS256 admin JWT by reading `(id, session_stamp)` from the read-only `/app/openwebui_data/webui.db` SQLite database and signing with `WEBUI_SECRET_KEY` (or uses `OPENWEBUI_API_KEY` if configured). For every entry in `runtime.describe_agents()`, it upserts a hidden base model record (`id="<agent_id>"`, `base_model_id=None`, `meta.hidden=True`) and a visible Workspace Model preset (`id="agent.<agent_id>"`, `base_model_id="<agent_id>"`, `meta.hidden=False`) with public read access grants, removes stale `agent.*` entries, and aligns `DEFAULT_MODELS` to `"agent.oap"`.

**Tech Stack:** Python 3.12, FastAPI, Pydantic Settings, `httpx`, `PyJWT` / stdlib HMAC-SHA256 JWT, `sqlite3` (read-only URI mode), `pytest`.

## Global Constraints

- Never modify `/app/openwebui_data/webui.db` directly; open it strictly in read-only URI mode (`file:...?mode=ro`).
- All mutations in Open WebUI go through its HTTP API (`http://open-webui:8080/api/v1/...`).
- When Open WebUI is unreachable, `webui.db` is missing/has no admin account, or `WEBUI_SECRET_KEY` is unset, `sync_models_to_openwebui()` must return `{"status": "skipped", "reason": ...}` without raising.
- Run `graphify update .` after modifying code files.

---

### Task 1: Settings, Enriched Agent Descriptions, and Bridge Prefix Normalisation

**Files:**
- Modify: `app/settings.py:66-75`
- Modify: `app/agents/runtime.py:459-500`
- Modify: `app/bridge/openwebui_router.py:40-175`
- Test: `tests/test_bridge.py`

**Interfaces:**
- Produces:
  - `settings.openwebui_base_url: str`
  - `settings.openwebui_api_key: str`
  - `settings.webui_secret_key: str`
  - `settings.openwebui_auto_sync: bool`
  - `runtime.describe_agents() -> list[dict[str, str]]` (each entry has `id`, `name`, `kind`, `description`)
  - `openwebui_router.normalise_model_id(model: str | None) -> str`

- [ ] **Step 1: Write the failing tests in `tests/test_bridge.py`**

Add tests verifying `normalise_model_id` strips `"agent."` and `"oap."` prefixes and `/v1/chat/completions` resolves `"agent.platform-builder"` to `"platform-builder"`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with-requirements requirements-dev.txt pytest tests/test_bridge.py -k "agent_prefix or normalise_model_id" -v`
Expected: FAIL (`ImportError` or `AssertionError`)

- [ ] **Step 3: Implement settings, `describe_agents` description field, and `normalise_model_id`**

Update `app/settings.py`, `app/agents/runtime.py`, and `app/bridge/openwebui_router.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --with-requirements requirements-dev.txt pytest tests/test_bridge.py -v`
Expected: PASS

---

### Task 2: Open WebUI Sync Client (`app/bridge/openwebui_sync.py`)

**Files:**
- Create: `app/bridge/openwebui_sync.py`
- Test: `tests/test_openwebui_sync.py`

**Interfaces:**
- Consumes: `settings` from `app/settings.py`, `runtime.describe_agents()` from `app/agents/runtime.py`
- Produces:
  - `resolve_openwebui_token(db_path: Path | None = None, secret_key: str | None = None, api_key: str | None = None) -> str | None`
  - `sync_models_to_openwebui(catalog: list[dict[str, str]] | None = None, *, client: httpx.Client | None = None, db_path: Path | None = None) -> dict[str, Any]`

- [ ] **Step 1: Write the failing unit tests in `tests/test_openwebui_sync.py`**

Test read-only SQLite `(id, session_stamp)` extraction, HS256 JWT minting, creating/updating base + `agent.<id>` workspace models, deleting stale `agent.<id>` models, updating `DEFAULT_MODELS` to `"agent.oap"`, and graceful `"skipped"` behavior when unconfigured or unreachable.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --with-requirements requirements-dev.txt pytest tests/test_openwebui_sync.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'app.bridge.openwebui_sync'`)

- [ ] **Step 3: Implement `app/bridge/openwebui_sync.py`**

Implement read-only SQLite admin lookup, HS256 JWT minting, `/api/v1/models/all` diffing, base model (`meta.hidden=True`) + workspace model (`id="agent.<id>"`, `base_model_id="<id>"`) upsert, stale cleanup, and `/api/v1/configs/models` default model alignment.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --with-requirements requirements-dev.txt pytest tests/test_openwebui_sync.py -v`
Expected: PASS

---

### Task 3: Platform Agent Tools & Runtime Lifecycle Integration

**Files:**
- Modify: `app/tools/platform_tools.py`
- Modify: `app/main.py`
- Modify: `app/bridge/openwebui_router.py`
- Test: `tests/test_openwebui_sync.py`

**Interfaces:**
- Consumes: `sync_models_to_openwebui` from `app/bridge/openwebui_sync.py`
- Produces:
  - `platform_tools.sync_openwebui_models() -> dict[str, Any]` in `REGISTRY_TOOLS` and `HEALTH_TOOLS`
  - Automatic `openwebui_sync` field returned by `create_or_update_agent` and `delete_custom_agent`
  - Startup and `_resync()` hooks in `app/main.py`

- [ ] **Step 1: Write failing tests for platform tool integration**

Add tests in `tests/test_openwebui_sync.py` verifying `sync_openwebui_models` is in `REGISTRY_TOOLS` and `HEALTH_TOOLS`, and `create_or_update_agent` / `delete_custom_agent` include `"openwebui_sync"` in their result.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --with-requirements requirements-dev.txt pytest tests/test_openwebui_sync.py -k "platform_tools" -v`
Expected: FAIL

- [ ] **Step 3: Wire `sync_openwebui_models` into `platform_tools.py`, `main.py`, and `openwebui_router.py`**

Add `sync_openwebui_models()` to `app/tools/platform_tools.py`, call it from `create_or_update_agent`, `delete_custom_agent`, and `inspect_platform_health`, and wire startup/resync hooks in `app/main.py` and lazy first-request sync in `app/bridge/openwebui_router.py`.

- [ ] **Step 4: Run full test suite, linter, graphify update, and live container verification**

Run:
- `uv run --with-requirements requirements-dev.txt pytest -q`
- `uv run --with-requirements requirements-dev.txt ruff check app tests scripts`
- `graphify update .`
Expected: All tests pass, ruff clean, graph updated.
