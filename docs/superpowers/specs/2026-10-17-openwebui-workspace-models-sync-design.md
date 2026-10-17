# Open WebUI Workspace Models Sync & Platform Agent Integration — Design Spec

**Date:** 2026-10-17  
**Status:** Approved

---

## 1. Problem & Goal

In Open WebUI v0.12.3, the **Workspace → Models** page (`GET /api/v1/models/list`) only displays rows from Open WebUI's `model` table where `base_model_id IS NOT NULL` (Workspace Model presets). Raw models advertised by the OAP `/v1/models` bridge (`oap`, `platform-builder`, `platform-engineer`, `platform-manager`, `platform-telemetry`, and custom/catalog agents) exist only as unconfigured upstream base models (`base_model_id = NULL`), causing:

1. **Workspace → Models** to show *"No models found"*.
2. Non-admin users (`role = "user"`) to have no read access grants to OAP agents unless an admin manually configures every model in **Admin → Settings → Models**.
3. Dynamically created or deleted agents (via **Platform Builder** or `app/catalog/`) to require manual steps in Open WebUI before appearing in **Workspace → Models**.

**Goal:** Automatically synchronize OAP's coordinator team, core agents, catalog entries, and custom builder agents into Open WebUI's **Workspace → Models** via Open WebUI's REST API, keep them in sync when agents are created or deleted at runtime, and expose a sync/inspection tool to **Platform Builder** and **Platform Manager**.

---

## 2. Architecture & Component Boundaries

```text
app/agents/store.py ──subscribe──▶ app/agents/runtime.py ──subscribe──▶ app/main.py
                                           │                                │
                                   describe_agents()                        ▼
                                           │                   app/bridge/openwebui_sync.py
                                           └───────────────────────▶        │
                                                                            │ HTTP (short-lived Admin JWT
                                                                            │ from read-only webui.db +
                                                                            │ WEBUI_SECRET_KEY)
                                                                            ▼
                                                               http://open-webui:8080/api/v1/models/*
```

### 2.1 Configuration (`app/settings.py`)
Add three settings to `Settings` (all backed by environment variables with sensible defaults):
- `openwebui_base_url: str = "http://open-webui:8080"` — Internal URL of the Open WebUI service on the Docker network.
- `openwebui_api_key: str = ""` — Optional explicit API key (`sk-...`) or bearer token for Open WebUI. When blank, `openwebui_sync` mints a short-lived admin JWT using `webui_secret_key` and `/app/openwebui_data/webui.db`.
- `webui_secret_key: str = ""` — Loaded from `WEBUI_SECRET_KEY` in `.env` (already present in `.env` and `.env.example`).
- `openwebui_auto_sync: bool = True` — Enables automatic Workspace Model synchronization on startup and agent registry changes.

### 2.2 Enriched Agent Catalogue (`app/agents/runtime.py`)
Extend `runtime.describe_agents()` so each entry includes a human-readable `description` alongside `id`, `name`, and `kind`:
- Coordinator (`id="oap"`): `description="Routes platform requests to the right specialist agent"`
- Core and dynamic agents: `description = getattr(agent, "role", "") or getattr(agent, "description", "") or ""`
- Catalog teams and workflows: `description = getattr(item, "description", "") or getattr(item, "role", "") or ""`

### 2.3 Open WebUI Sync Client (`app/bridge/openwebui_sync.py`)
A self-contained module responsible for reconciling `runtime.describe_agents()` with Open WebUI:

1. **Admin Credential Resolution (`resolve_openwebui_token()`):**
   - If `settings.openwebui_api_key.strip()` is set, return it directly.
   - Otherwise, open `/app/openwebui_data/webui.db` in read-only URI mode (`file:/app/openwebui_data/webui.db?mode=ro`) and query:
     ```sql
     SELECT u.id, a.session_stamp
     FROM "user" u
     JOIN auth a ON a.id = u.id
     WHERE u.role = 'admin' AND a.active = 1
     ORDER BY u.created_at ASC
     LIMIT 1
     ```
   - Mint an HS256 JWT signed with `settings.webui_secret_key` containing `{"id": user_id, "session_stamp": session_stamp, "jti": str(uuid4()), "iat": now, "exp": now + 60}`.
   - If `webui.db` does not exist yet, has no admin user yet, or `webui_secret_key` is empty/placeholder, return `None` so sync skips cleanly.

2. **Two-Layer Model Registration (`sync_models_to_openwebui()`):**
   Open WebUI's `/api/v1/models/list` (Workspace → Models) requires `base_model_id IS NOT NULL` and ignores any preset whose `id` matches a raw `/v1/models` ID. For each agent entry `{id, name, kind, description}`:
   - **Base Model Record (`id = entry["id"]`, `base_model_id = None`):**
     - Upserted via `/api/v1/models/create` or `/api/v1/models/model/update`.
     - `meta = {"hidden": True, "description": entry["description"], "tags": [{"name": "OAP"}, {"name": entry["kind"]}]}`
     - `access_grants`: public read grant (`principal_type="user"`, `principal_id="*"`, `permission="read"`).
     - Marking the base model `hidden: True` keeps it active in `request.app.state.MODELS` for routing while hiding the raw duplicate from the chat model picker.
   - **Workspace Model Preset (`id = f"agent.{entry['id']}"`, `base_model_id = entry["id"]`):**
     - Upserted via `/api/v1/models/create` or `/api/v1/models/model/update`.
     - `name = entry["name"]`
     - `meta = {"hidden": False, "description": entry["description"], "tags": [{"name": "OAP"}, {"name": entry["kind"]}], "capabilities": {"file_upload": True, "citations": True}}`
     - `params = {}`
     - `access_grants`: public read grant (`principal_type="user"`, `principal_id="*"`, `permission="read"`).

3. **Stale OAP Workspace Model Cleanup:**
   - Fetch existing models in a single call via `GET /api/v1/models/all`.
   - Any model whose `id` starts with `"agent."` and carries the `"OAP"` tag, where the underlying `<agent_id>` is no longer in `describe_agents()`, is deleted via `POST /api/v1/models/model/delete` with JSON body `{"id": model_id}` (along with its stale base model record).
   - User-created Workspace Models that do not start with `"agent."` are never modified or deleted.

4. **Default Model Alignment:**
   - Ensure Open WebUI's `DEFAULT_MODELS` config (`GET/POST /api/v1/configs/models`) points to `"agent.oap"` when unset or when still set to stale `"agno"` or `"oap"`.

### 2.4 Bridge Prefix Tolerance (`app/bridge/openwebui_router.py`)
- Add `normalise_model_id(model: str | None) -> str`:
  - Strips a leading `"agent."` or `"oap."` prefix if a caller ever passes the Workspace Model ID directly to `/v1/chat/completions` or `/v1/files/route` (Open WebUI itself already rewrites `payload["model"] = model_info.base_model_id`, so this provides defense-in-depth).

### 2.5 Platform Agent Tools & Lifecycle Hooks
- **`app/tools/platform_tools.py`:**
  - Add `sync_openwebui_models() -> dict[str, Any]` to `REGISTRY_TOOLS` and `HEALTH_TOOLS`.
  - Call `sync_openwebui_models()` automatically inside `create_or_update_agent()` and `delete_custom_agent()` and include `"openwebui_sync"` in their return payloads so **Platform Builder** can report the Workspace Model status directly in chat.
  - Include Open WebUI sync status in `inspect_platform_health()`.
- **`app/main.py`:**
  - In `_resync()`, call `sync_openwebui_models()` whenever `runtime` rebuilds agents.
  - In `lifespan()`, spawn a background task on startup that waits briefly for Open WebUI to become reachable and runs `sync_openwebui_models()`.

---

## 3. Error Handling & Edge Cases

- **Headless Mode (`make oap`) / Fresh Install (No Admin Account Yet):**
  - When `webui.db` is missing, has zero admin rows (before the first user signs up at `http://localhost:3000`), or `http://open-webui:8080` is unreachable, `sync_openwebui_models()` returns `{"status": "skipped", "reason": "..."}` without raising an exception.
- **Lazy First-Request Sync:**
  - If the startup sync skipped because no admin account existed yet when the containers booted, the first `/v1/models` or `/v1/chat/completions` call from Open WebUI (which happens right after the admin account is created) triggers a one-time background sync if not yet synced.
- **User Customizations Preserved:**
  - When updating an existing `agent.<id>` Workspace Model, if an admin customized `params` (e.g., temperature or custom system prompt) in Open WebUI, existing `params` are preserved while `name`, `base_model_id`, and `meta.description` are refreshed.

---

## 4. Testing Strategy

1. **`tests/test_openwebui_sync.py`:**
   - Temp SQLite `webui.db` fixture with `user` and `auth` tables; verifies read-only `(id, session_stamp)` extraction and valid HS256 JWT claims (`id`, `session_stamp`, `jti`, `iat`, `exp`).
   - Mocked `httpx.Client` / transport verifying:
     - Creation of hidden base models (`id="<id>"`, `base_model_id=None`, `meta.hidden=True`) and visible Workspace Models (`id="agent.<id>"`, `base_model_id="<id>"`, `meta.hidden=False`).
     - Update path when models already exist in Open WebUI.
     - Deletion of removed `agent.<id>` models when a custom agent is deleted.
     - Update of `DEFAULT_MODELS` from `"agno"` / `"oap"` to `"agent.oap"`.
     - Graceful `"skipped"` status when `webui.db` has no admin user or Open WebUI is unreachable.
2. **`tests/test_bridge.py`:**
   - Verify `normalise_model_id("agent.oap") == "oap"` and `/v1/chat/completions` accepts `"agent.platform-builder"`.
