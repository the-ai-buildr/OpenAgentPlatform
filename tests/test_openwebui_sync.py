"""Tests for synchronizing OAP agents into Open WebUI Workspace Models."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.bridge.openwebui_sync import (
    PUBLIC_READ_GRANT,
    WORKSPACE_MODEL_PREFIX,
    resolve_openwebui_token,
    sync_models_to_openwebui,
)


def _create_webui_db(path: Path, *, with_admin: bool = True, active: int = 1) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE user (id TEXT PRIMARY KEY, role TEXT, created_at INTEGER)")
    conn.execute("CREATE TABLE auth (id TEXT PRIMARY KEY, active INTEGER, session_stamp TEXT)")
    if with_admin:
        conn.execute(
            "INSERT INTO user (id, role, created_at) VALUES (?, ?, ?)",
            ("admin-123", "admin", 1700000000),
        )
        conn.execute(
            "INSERT INTO auth (id, active, session_stamp) VALUES (?, ?, ?)",
            ("admin-123", active, "stamp-abc"),
        )
    conn.commit()
    conn.close()


def _decode_hs256_jwt(token: str, secret: str) -> dict[str, Any]:
    header_b64, payload_b64, sig_b64 = token.split(".")
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    expected_sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    expected_b64 = base64.urlsafe_b64encode(expected_sig).rstrip(b"=").decode("ascii")
    assert sig_b64 == expected_b64
    padding = "=" * (-len(payload_b64) % 4)
    return json.loads(base64.urlsafe_b64decode(payload_b64 + padding).decode("utf-8"))


class TestResolveOpenWebUIToken:
    def test_prefers_explicit_api_key(self, tmp_path: Path):
        token = resolve_openwebui_token(
            db_path=tmp_path / "missing.db",
            secret_key="secret",
            api_key="sk-explicit-key",
        )
        assert token == "sk-explicit-key"

    def test_mints_hs256_jwt_from_readonly_sqlite(self, tmp_path: Path):
        db_path = tmp_path / "webui.db"
        _create_webui_db(db_path)
        token = resolve_openwebui_token(db_path=db_path, secret_key="my-webui-secret", api_key="")
        assert token is not None
        claims = _decode_hs256_jwt(token, "my-webui-secret")
        assert claims["id"] == "admin-123"
        assert claims["session_stamp"] == "stamp-abc"
        assert "jti" in claims
        assert claims["exp"] > claims["iat"]

    def test_returns_none_when_db_missing_or_no_admin_or_empty_secret(self, tmp_path: Path):
        db_path = tmp_path / "webui.db"
        _create_webui_db(db_path, with_admin=False)
        assert resolve_openwebui_token(db_path=db_path, secret_key="my-secret", api_key="") is None
        assert (
            resolve_openwebui_token(
                db_path=tmp_path / "nonexistent.db", secret_key="my-secret", api_key=""
            )
            is None
        )
        _create_webui_db(tmp_path / "admin.db", with_admin=True)
        assert (
            resolve_openwebui_token(
                db_path=tmp_path / "admin.db",
                secret_key="   ",
                api_key="",
            )
            is None
        )


class FakeOpenWebUI:
    def __init__(
        self,
        existing_models: list[dict[str, Any]] | None = None,
        models_config: dict[str, Any] | None = None,
    ) -> None:
        self.models: dict[str, dict[str, Any]] = {m["id"]: dict(m) for m in (existing_models or [])}
        self.models_config: dict[str, Any] = models_config or {
            "DEFAULT_MODELS": "agno",
            "DEFAULT_PINNED_MODELS": None,
            "MODEL_ORDER_LIST": [],
        }
        self.created: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self.config_updates: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/models/all" and request.method == "GET":
            return httpx.Response(200, json=list(self.models.values()))
        if path == "/api/v1/models/create" and request.method == "POST":
            payload = json.loads(request.content.decode("utf-8"))
            self.models[payload["id"]] = payload
            self.created.append(payload)
            return httpx.Response(200, json=payload)
        if path == "/api/v1/models/model/update" and request.method == "POST":
            payload = json.loads(request.content.decode("utf-8"))
            self.models[payload["id"]] = payload
            self.updated.append(payload)
            return httpx.Response(200, json=payload)
        if path == "/api/v1/models/model/delete" and request.method == "POST":
            payload = json.loads(request.content.decode("utf-8"))
            model_id = payload["id"]
            self.models.pop(model_id, None)
            self.deleted.append(model_id)
            return httpx.Response(200, json=True)
        if path == "/api/v1/configs/models" and request.method == "GET":
            return httpx.Response(200, json=self.models_config)
        if path == "/api/v1/configs/models" and request.method == "POST":
            payload = json.loads(request.content.decode("utf-8"))
            self.models_config.update(payload)
            self.config_updates.append(payload)
            return httpx.Response(200, json=self.models_config)
        return httpx.Response(404, json={"detail": f"Unhandled {request.method} {path}"})


class TestSyncModelsToOpenWebUI:
    def test_creates_hidden_base_and_visible_workspace_models(self, tmp_path: Path):
        db_path = tmp_path / "webui.db"
        _create_webui_db(db_path)
        fake = FakeOpenWebUI()
        catalog = [
            {
                "id": "oap",
                "name": "Platform Coordinator",
                "kind": "team",
                "description": "Routes platform work to the right specialist agent",
            },
            {
                "id": "platform-builder",
                "name": "Platform Builder",
                "kind": "agent",
                "description": "Creates and configures specialist agents",
            },
        ]

        with httpx.Client(
            transport=httpx.MockTransport(fake.handler), base_url="http://open-webui:8080"
        ) as client:
            result = sync_models_to_openwebui(
                catalog,
                client=client,
                db_path=db_path,
                secret_key="test-webui-secret",
            )

        assert result["status"] == "ok"
        assert set(result["workspace_models"]) == {"agent.oap", "agent.platform-builder"}

        # Base models are hidden so the model picker does not show duplicates.
        base_oap = fake.models["oap"]
        assert base_oap["base_model_id"] is None
        assert base_oap["meta"]["hidden"] is True
        assert base_oap["access_grants"] == [PUBLIC_READ_GRANT]

        # Workspace models have base_model_id set so Workspace -> Models lists them.
        ws_oap = fake.models[f"{WORKSPACE_MODEL_PREFIX}oap"]
        assert ws_oap["base_model_id"] == "oap"
        assert ws_oap["name"] == "Platform Coordinator"
        assert ws_oap["meta"]["hidden"] is False
        assert ws_oap["meta"]["description"] == "Routes platform work to the right specialist agent"
        assert ws_oap["access_grants"] == [PUBLIC_READ_GRANT]

        # Stale default model ("agno") is updated to "agent.oap".
        assert fake.models_config["DEFAULT_MODELS"] == "agent.oap"

    def test_updates_existing_and_deletes_stale_oap_models_only(self, tmp_path: Path):
        db_path = tmp_path / "webui.db"
        _create_webui_db(db_path)
        existing = [
            {
                "id": "oap",
                "base_model_id": None,
                "name": "Old Name",
                "meta": {"hidden": True, "tags": [{"name": "OAP"}]},
                "params": {},
            },
            {
                "id": "agent.oap",
                "base_model_id": "oap",
                "name": "Old Name",
                "meta": {"hidden": False, "tags": [{"name": "OAP"}]},
                "params": {"temperature": 0.2},
            },
            {
                "id": "old-custom",
                "base_model_id": None,
                "name": "Old Custom",
                "meta": {"hidden": True, "tags": [{"name": "OAP"}]},
                "params": {},
            },
            {
                "id": "agent.old-custom",
                "base_model_id": "old-custom",
                "name": "Old Custom",
                "meta": {"hidden": False, "tags": [{"name": "OAP"}]},
                "params": {},
            },
            {
                "id": "user-custom-preset",
                "base_model_id": "oap",
                "name": "My Personal Preset",
                "meta": {"hidden": False, "tags": [{"name": "personal"}]},
                "params": {"system": "custom"},
            },
        ]
        fake = FakeOpenWebUI(existing_models=existing)
        catalog = [
            {
                "id": "oap",
                "name": "Platform Coordinator",
                "kind": "team",
                "description": "Routes platform work",
            }
        ]

        with httpx.Client(
            transport=httpx.MockTransport(fake.handler), base_url="http://open-webui:8080"
        ) as client:
            result = sync_models_to_openwebui(
                catalog,
                client=client,
                db_path=db_path,
                secret_key="test-webui-secret",
            )

        assert result["status"] == "ok"
        assert set(fake.deleted) == {"agent.old-custom", "old-custom"}
        assert "user-custom-preset" in fake.models
        assert fake.models["agent.oap"]["name"] == "Platform Coordinator"
        assert fake.models["agent.oap"]["params"] == {"temperature": 0.2}

    def test_tags_models_with_their_platform_or_project_group(self, tmp_path: Path):
        db_path = tmp_path / "webui.db"
        _create_webui_db(db_path)
        fake = FakeOpenWebUI()
        catalog = [
            {"id": "oap", "name": "Platform Coordinator", "kind": "team", "group": "platform"},
            {"id": "platform-skills", "name": "Platform Skills", "kind": "agent", "group": "platform"},
            {"id": "project-dev", "name": "Project Dev", "kind": "team", "group": "project"},
            {"id": "project-researcher", "name": "Researcher", "kind": "agent", "group": "project"},
            {"id": "acme", "name": "Acme", "kind": "team", "group": "project"},
            {"id": "research", "name": "Research", "kind": "agent"},
        ]

        with httpx.Client(
            transport=httpx.MockTransport(fake.handler), base_url="http://open-webui:8080"
        ) as client:
            result = sync_models_to_openwebui(
                catalog, client=client, db_path=db_path, secret_key="test-webui-secret"
            )

        assert result["status"] == "ok"

        def tag_names(model_id: str) -> list[str]:
            return [t["name"] for t in fake.models[model_id]["meta"]["tags"]]

        expected = {
            "oap": ["OAP", "team", "platform"],
            "platform-skills": ["OAP", "agent", "platform"],
            "project-dev": ["OAP", "team", "project"],
            "project-researcher": ["OAP", "agent", "project"],
            "acme": ["OAP", "team", "project"],
            "research": ["OAP", "agent"],
        }
        for agent_id, tags in expected.items():
            assert tag_names(agent_id) == tags
            assert tag_names(f"{WORKSPACE_MODEL_PREFIX}{agent_id}") == tags

    def test_group_matching_kind_is_not_duplicated(self, tmp_path: Path):
        db_path = tmp_path / "webui.db"
        _create_webui_db(db_path)
        fake = FakeOpenWebUI()
        with httpx.Client(
            transport=httpx.MockTransport(fake.handler), base_url="http://open-webui:8080"
        ) as client:
            sync_models_to_openwebui(
                [{"id": "oap", "kind": "team", "group": "team"}],
                client=client,
                db_path=db_path,
                secret_key="test-webui-secret",
            )
        assert [t["name"] for t in fake.models["agent.oap"]["meta"]["tags"]] == ["OAP", "team"]

    def test_skips_cleanly_when_unauthenticated_or_empty_catalog(self, tmp_path: Path):
        res_empty = sync_models_to_openwebui([], db_path=tmp_path / "missing.db", secret_key="")
        assert res_empty["status"] == "skipped"

        res_no_token = sync_models_to_openwebui(
            [{"id": "oap", "name": "Platform Coordinator", "kind": "team"}],
            db_path=tmp_path / "missing.db",
            secret_key="",
            api_key="",
        )
        assert res_no_token["status"] == "skipped"


class TestPlatformToolsIntegration:
    def test_sync_openwebui_models_is_exposed_in_platform_toolsets(self):
        from app.tools import platform_tools

        assert platform_tools.sync_openwebui_models in platform_tools.REGISTRY_TOOLS
        assert platform_tools.sync_openwebui_models in platform_tools.HEALTH_TOOLS

    def test_create_and_delete_agent_report_openwebui_sync_status(
        self, registry_file: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from app.tools import platform_tools

        monkeypatch.setattr(
            platform_tools,
            "sync_openwebui_models",
            lambda: {"status": "ok", "workspace_models": ["agent.oap", "agent.research"]},
        )

        created = platform_tools.create_or_update_agent(
            agent_id="research",
            name="Research Analyst",
            role="Summarises market research",
        )
        assert created["status"] == "ok"
        assert created["openwebui_sync"]["status"] == "ok"
        assert "agent.research" in created["openwebui_sync"]["workspace_models"]

        deleted = platform_tools.delete_custom_agent("research")
        assert deleted["status"] == "ok"
        assert deleted["openwebui_sync"]["status"] == "ok"

    def test_create_and_delete_project_report_openwebui_sync_status(
        self, registry_file: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from app.tools import platform_tools

        monkeypatch.setattr(
            platform_tools,
            "sync_openwebui_models",
            lambda: {"status": "ok", "workspace_models": ["agent.oap", "agent.acme"]},
        )

        created = platform_tools.create_or_update_project(project_id="acme", name="Acme")
        assert created["status"] == "ok"
        assert "agent.acme" in created["openwebui_sync"]["workspace_models"]

        deleted = platform_tools.delete_project("acme")
        assert deleted["status"] == "ok"
        assert deleted["openwebui_sync"]["status"] == "ok"


class TestSyncKnowledgeWithOpenWebUI:
    def test_pulls_openwebui_knowledge_dirs_and_user_folders_to_workspace_and_pushes_local_files(
        self, tmp_path: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from app.bridge.openwebui_sync import sync_knowledge_with_openwebui
        from app.knowledge import knowledge_manager as km

        uploads_dir = tmp_path / "uploads"
        uploads_dir.mkdir()
        monkeypatch.setattr(km, "OPENWEBUI_UPLOADS_DIR", uploads_dir)

        # Create two uploaded files in Open WebUI's uploads dir
        nda_upload = uploads_dir / "11111111-1111-1111-1111-111111111111_acme_nda.txt"
        nda_upload.write_text("Confidential NDA terms.", encoding="utf-8")

        user_note_upload = uploads_dir / "22222222-2222-2222-2222-222222222222_notes.md"
        user_note_upload.write_text("# Alice Personal Notes\n", encoding="utf-8")

        # Also create a local file on disk in workspace_files/sops/onboarding.md to push to Open WebUI
        km.save_text_file("sops", "onboarding.md", "# Onboarding SOP\nWelcome!", auto_index=False)

        # Build webui.db with user, auth, knowledge, knowledge_directory, knowledge_file, file, and folder tables
        db_path = tmp_path / "webui.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE user (id TEXT PRIMARY KEY, name TEXT, email TEXT, role TEXT, created_at INTEGER)"
        )
        conn.execute("CREATE TABLE auth (id TEXT PRIMARY KEY, active INTEGER, session_stamp TEXT)")
        conn.execute(
            "CREATE TABLE knowledge (id TEXT PRIMARY KEY, user_id TEXT, name TEXT, description TEXT, meta TEXT)"
        )
        conn.execute(
            "CREATE TABLE knowledge_directory (id TEXT PRIMARY KEY, knowledge_id TEXT, parent_id TEXT, name TEXT, user_id TEXT)"
        )
        conn.execute(
            "CREATE TABLE knowledge_file (id TEXT PRIMARY KEY, knowledge_id TEXT, file_id TEXT, directory_id TEXT, user_id TEXT)"
        )
        conn.execute("CREATE TABLE file (id TEXT PRIMARY KEY, user_id TEXT, filename TEXT, path TEXT, meta TEXT)")
        conn.execute(
            "CREATE TABLE folder (id TEXT PRIMARY KEY, parent_id TEXT, user_id TEXT, name TEXT, items TEXT, data TEXT)"
        )

        conn.execute(
            "INSERT INTO user VALUES (?, ?, ?, ?, ?)",
            ("admin-123", "Admin", "admin@example.com", "admin", 1700000000),
        )
        conn.execute("INSERT INTO auth VALUES (?, ?, ?)", ("admin-123", 1, "stamp-abc"))
        conn.execute(
            "INSERT INTO user VALUES (?, ?, ?, ?, ?)",
            ("user-alice", "Alice", "alice@example.com", "user", 1700000001),
        )

        # Admin Knowledge Base "Contracts" with subdirectory "NDAs" and file "acme_nda.txt"
        conn.execute(
            "INSERT INTO knowledge VALUES (?, ?, ?, ?, ?)",
            ("kb-contracts", "admin-123", "Contracts", "Company contracts", "{}"),
        )
        conn.execute(
            "INSERT INTO knowledge_directory VALUES (?, ?, ?, ?, ?)",
            ("dir-ndas", "kb-contracts", None, "NDAs", "admin-123"),
        )
        conn.execute(
            "INSERT INTO file VALUES (?, ?, ?, ?, ?)",
            ("file-nda", "admin-123", "acme_nda.txt", str(nda_upload), "{}"),
        )
        conn.execute(
            "INSERT INTO knowledge_file VALUES (?, ?, ?, ?, ?)",
            ("kf-1", "kb-contracts", "file-nda", "dir-ndas", "admin-123"),
        )

        # User Alice's chat folder "Research Notes" referencing file-notes in data.files
        conn.execute(
            "INSERT INTO file VALUES (?, ?, ?, ?, ?)",
            ("file-notes", "user-alice", "notes.md", str(user_note_upload), "{}"),
        )
        conn.execute(
            "INSERT INTO folder VALUES (?, ?, ?, ?, ?, ?)",
            (
                "folder-alice-1",
                None,
                "user-alice",
                "Research Notes",
                "{}",
                json.dumps({"files": [{"type": "file", "id": "file-notes", "name": "notes.md"}]}),
            ),
        )
        conn.commit()
        conn.close()

        created_kbs: list[dict[str, Any]] = []
        uploaded_files: list[str] = []
        added_kb_files: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/api/v1/knowledge/" and request.method == "GET":
                return httpx.Response(200, json={"items": created_kbs, "total": len(created_kbs)})
            if path == "/api/v1/knowledge/create" and request.method == "POST":
                payload = json.loads(request.content.decode("utf-8"))
                kb_obj = {"id": f"kb-{len(created_kbs) + 1}", **payload, "files": []}
                created_kbs.append(kb_obj)
                return httpx.Response(200, json=kb_obj)
            if path == "/api/v1/files/" and request.method == "POST":
                file_id = f"uploaded-{len(uploaded_files) + 1}"
                uploaded_files.append(file_id)
                return httpx.Response(200, json={"id": file_id, "filename": "onboarding.md"})
            if path.endswith("/file/add") and request.method == "POST":
                kb_id = path.split("/")[4]
                payload = json.loads(request.content.decode("utf-8"))
                added_kb_files.append((kb_id, payload["file_id"]))
                return httpx.Response(200, json={"id": kb_id})
            return httpx.Response(200, json={})

        with httpx.Client(
            transport=httpx.MockTransport(handler), base_url="http://open-webui:8080"
        ) as client:
            result = sync_knowledge_with_openwebui(
                client=client,
                db_path=db_path,
                secret_key="test-webui-secret",
            )

        assert result["status"] == "ok"
        # 1. Pulled from Open WebUI knowledge + nested directory -> workspace_files/contracts/ndas/acme_nda.txt
        staged_nda = workspace / "contracts" / "ndas" / "acme-nda.txt"
        assert staged_nda.is_file()
        assert staged_nda.read_text(encoding="utf-8") == "Confidential NDA terms."

        # 2. Pulled from user Alice's chat folder -> workspace_files/users/alice/research-notes/notes.md
        staged_user_note = workspace / "users" / "alice" / "research-notes" / "notes.md"
        assert staged_user_note.is_file()
        assert "Alice Personal Notes" in staged_user_note.read_text(encoding="utf-8")

        # 3. Pushed local workspace_files/sops/onboarding.md -> Open WebUI Knowledge Base
        assert any(kb["name"] == "sops" for kb in created_kbs)
        assert len(uploaded_files) >= 1
        assert len(added_kb_files) >= 1

