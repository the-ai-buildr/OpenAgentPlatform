"""Synchronize OAP agents into Open WebUI's Workspace -> Models registry.

Open WebUI's `/api/v1/models/list` page (Workspace -> Models) only displays rows
in its `model` table where `base_model_id` is set. Raw `/v1/models` entries from
the bridge are treated as unconfigured upstream base models (`base_model_id=None`).

This module:
1. Reads the active admin `(id, session_stamp)` from the read-only mounted
   `/app/openwebui_data/webui.db` and mints a short-lived HS256 JWT signed with
   `WEBUI_SECRET_KEY` (or uses `OPENWEBUI_API_KEY` if explicitly configured).
2. Registers each OAP agent as:
   - a hidden base model (`id="<agent_id>"`, `base_model_id=None`, `meta.hidden=True`)
     with public read access grants so non-admin users can run it without seeing
     duplicate entries in the chat model picker; and
   - a visible Workspace Model preset (`id="agent.<agent_id>"`, `base_model_id="<agent_id>"`)
     with the agent's display name, role description, tags, and public read access.
3. Deletes stale `agent.<id>` models when custom agents are removed from OAP.
4. Aligns Open WebUI's `DEFAULT_MODELS` to `"agent.oap"` when unset or still
   pointing at `"agno"` / `"oap"`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from app.settings import OPENWEBUI_UPLOADS_DIR, settings

logger = logging.getLogger(__name__)

OPENWEBUI_DB_PATH = OPENWEBUI_UPLOADS_DIR.parent / "webui.db"
WORKSPACE_MODEL_PREFIX = "agent."
DEFAULT_TEAM_ID = "oap"
OAP_TAG = "OAP"

PUBLIC_READ_GRANT: dict[str, str] = {
    "principal_type": "user",
    "principal_id": "*",
    "permission": "read",
}

_last_sync_result: dict[str, Any] = {"status": "never_run"}
_bg_lock = threading.Lock()
_bg_running = False


def get_last_sync_result() -> dict[str, Any]:
    return dict(_last_sync_result)


def _default_catalog() -> list[dict[str, str]]:
    from app.agents import runtime

    return runtime.describe_agents()


def _default_team_id() -> str:
    from app.agents import runtime

    return runtime.TEAM_ID


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _mint_hs256_jwt(payload: dict[str, Any], secret: str) -> str:
    header_b64 = _b64url(
        json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode("utf-8")
    )
    payload_b64 = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    signature = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{header_b64}.{payload_b64}.{_b64url(signature)}"


def _is_usable_secret(secret: str) -> bool:
    return bool(secret.strip())


def resolve_openwebui_token(
    *,
    db_path: Path | None = None,
    secret_key: str | None = None,
    api_key: str | None = None,
) -> str | None:
    """Return a bearer token capable of calling Open WebUI's admin endpoints."""
    explicit_key = (api_key if api_key is not None else settings.openwebui_api_key).strip()
    if explicit_key:
        return explicit_key

    secret = (secret_key if secret_key is not None else settings.webui_secret_key).strip()
    if not _is_usable_secret(secret):
        return None

    target_db = db_path if db_path is not None else OPENWEBUI_DB_PATH
    if not target_db.is_file():
        return None

    try:
        uri = f"file:{target_db.resolve()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=2.0) as conn:
            row = conn.execute(
                'SELECT u.id, a.session_stamp FROM "user" u '
                "JOIN auth a ON a.id = u.id "
                "WHERE u.role = 'admin' AND a.active = 1 "
                "ORDER BY u.created_at ASC LIMIT 1"
            ).fetchone()
    except sqlite3.Error as exc:
        logger.debug("Could not read admin session from %s: %s", target_db, exc)
        return None

    if not row or not row[0]:
        return None

    now = int(time.time())
    claims: dict[str, Any] = {
        "id": str(row[0]),
        "session_stamp": row[1],
        "jti": str(uuid.uuid4()),
        "iat": now,
        "exp": now + 60,
    }
    return _mint_hs256_jwt(claims, secret)


def _has_oap_tag(model: dict[str, Any]) -> bool:
    meta = model.get("meta")
    if not isinstance(meta, dict):
        return False
    tags = meta.get("tags")
    if not isinstance(tags, list):
        return False
    for tag in tags:
        name = tag.get("name") if isinstance(tag, dict) else str(tag)
        if name == OAP_TAG:
            return True
    return False


def _base_model_payload(
    entry: dict[str, str], existing: dict[str, Any] | None = None
) -> dict[str, Any]:
    existing_params = (
        dict(existing.get("params") or {})
        if isinstance(existing, dict) and isinstance(existing.get("params"), dict)
        else {}
    )
    kind = entry.get("kind") or "agent"
    return {
        "id": entry["id"],
        "base_model_id": None,
        "name": entry.get("name") or entry["id"],
        "meta": {
            "hidden": True,
            "description": entry.get("description") or "",
            "tags": [{"name": OAP_TAG}, {"name": kind}],
        },
        "params": existing_params,
        "access_grants": [PUBLIC_READ_GRANT],
        "is_active": True,
    }


def _workspace_model_payload(
    entry: dict[str, str], existing: dict[str, Any] | None = None
) -> dict[str, Any]:
    existing_params = (
        dict(existing.get("params") or {})
        if isinstance(existing, dict) and isinstance(existing.get("params"), dict)
        else {}
    )
    kind = entry.get("kind") or "agent"
    return {
        "id": f"{WORKSPACE_MODEL_PREFIX}{entry['id']}",
        "base_model_id": entry["id"],
        "name": entry.get("name") or entry["id"],
        "meta": {
            "hidden": False,
            "description": entry.get("description") or "",
            "tags": [{"name": OAP_TAG}, {"name": kind}],
            "capabilities": {
                "file_upload": True,
                "citations": True,
            },
        },
        "params": existing_params,
        "access_grants": [PUBLIC_READ_GRANT],
        "is_active": True,
    }


def _upsert_model(
    client: httpx.Client,
    headers: dict[str, str],
    payload: dict[str, Any],
    existing_by_id: dict[str, dict[str, Any]],
) -> str:
    endpoint = (
        "/api/v1/models/model/update"
        if payload["id"] in existing_by_id
        else "/api/v1/models/create"
    )
    response = client.post(endpoint, headers=headers, json=payload)
    response.raise_for_status()
    return "updated" if payload["id"] in existing_by_id else "created"


def sync_models_to_openwebui(
    catalog: list[dict[str, str]] | None = None,
    *,
    client: httpx.Client | None = None,
    db_path: Path | None = None,
    secret_key: str | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Reconcile OAP's agent catalogue with Open WebUI's Workspace Models."""
    global _last_sync_result

    entries = catalog if catalog is not None else _default_catalog()
    if not entries:
        result = {"status": "skipped", "reason": "no agents in catalog"}
        _last_sync_result = result
        return result

    token = resolve_openwebui_token(db_path=db_path, secret_key=secret_key, api_key=api_key)
    if not token:
        result = {
            "status": "skipped",
            "reason": "no Open WebUI admin session or secret key available yet",
        }
        _last_sync_result = result
        return result

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    owns_client = client is None
    http_client = client or httpx.Client(
        base_url=settings.openwebui_base_url.rstrip("/"), timeout=10.0
    )

    try:
        all_resp = http_client.get("/api/v1/models/all", headers=headers)
        all_resp.raise_for_status()
        existing_list = all_resp.json()
        existing_by_id: dict[str, dict[str, Any]] = {
            m["id"]: m
            for m in (existing_list if isinstance(existing_list, list) else [])
            if isinstance(m, dict) and m.get("id")
        }

        desired_base_ids: set[str] = set()
        desired_workspace_ids: list[str] = []
        created: list[str] = []
        updated: list[str] = []

        for entry in entries:
            agent_id = entry["id"]
            ws_id = f"{WORKSPACE_MODEL_PREFIX}{agent_id}"
            desired_base_ids.add(agent_id)
            desired_workspace_ids.append(ws_id)

            base_payload = _base_model_payload(entry, existing_by_id.get(agent_id))
            base_action = _upsert_model(http_client, headers, base_payload, existing_by_id)
            if base_action == "created":
                created.append(agent_id)
            else:
                updated.append(agent_id)

            ws_payload = _workspace_model_payload(entry, existing_by_id.get(ws_id))
            ws_action = _upsert_model(http_client, headers, ws_payload, existing_by_id)
            if ws_action == "created":
                created.append(ws_id)
            else:
                updated.append(ws_id)

        deleted: list[str] = []
        for model_id, model in list(existing_by_id.items()):
            if not _has_oap_tag(model):
                continue
            if model_id.startswith(WORKSPACE_MODEL_PREFIX):
                if model_id not in desired_workspace_ids:
                    del_resp = http_client.post(
                        "/api/v1/models/model/delete", headers=headers, json={"id": model_id}
                    )
                    if del_resp.is_success:
                        deleted.append(model_id)
            elif model.get("base_model_id") is None and model_id not in desired_base_ids:
                del_resp = http_client.post(
                    "/api/v1/models/model/delete", headers=headers, json={"id": model_id}
                )
                if del_resp.is_success:
                    deleted.append(model_id)

        team_id = _default_team_id() if catalog is None else DEFAULT_TEAM_ID
        default_ws_id = f"{WORKSPACE_MODEL_PREFIX}{team_id}"
        cfg_resp = http_client.get("/api/v1/configs/models", headers=headers)
        if cfg_resp.is_success:
            cfg = cfg_resp.json() if isinstance(cfg_resp.json(), dict) else {}
            current_default = (cfg.get("DEFAULT_MODELS") or "").strip()
            if current_default in ("", "agno", team_id):
                cfg_payload = {
                    "DEFAULT_MODELS": default_ws_id
                    if default_ws_id in desired_workspace_ids
                    else current_default,
                    "DEFAULT_PINNED_MODELS": cfg.get("DEFAULT_PINNED_MODELS"),
                    "MODEL_ORDER_LIST": cfg.get("MODEL_ORDER_LIST") or [],
                    "DEFAULT_MODEL_METADATA": cfg.get("DEFAULT_MODEL_METADATA"),
                    "DEFAULT_MODEL_PARAMS": cfg.get("DEFAULT_MODEL_PARAMS"),
                }
                http_client.post("/api/v1/configs/models", headers=headers, json=cfg_payload)

        result = {
            "status": "ok",
            "workspace_models": desired_workspace_ids,
            "created": created,
            "updated": updated,
            "deleted": deleted,
        }
        _last_sync_result = result
        return result
    except Exception as exc:
        logger.debug("Open WebUI model sync skipped: %s", exc)
        result = {"status": "skipped", "reason": f"{type(exc).__name__}: {exc}"}
        _last_sync_result = result
        return result
    finally:
        if owns_client:
            http_client.close()


def ensure_synced_in_background(*, retries: int = 1, delay_seconds: float = 2.0) -> None:
    """Trigger a non-blocking Workspace Model sync if not already synced."""
    global _bg_running
    if not settings.openwebui_auto_sync or not settings.llm_configured:
        return
    with _bg_lock:
        if _bg_running:
            return
        _bg_running = True

    def _worker() -> None:
        global _bg_running
        try:
            for attempt in range(max(1, retries)):
                res = sync_models_to_openwebui()
                if res.get("status") == "ok" or attempt + 1 >= retries:
                    break
                time.sleep(delay_seconds)
        finally:
            with _bg_lock:
                _bg_running = False

    threading.Thread(target=_worker, name="openwebui-model-sync", daemon=True).start()
