"""Tests for Platform Docs path helpers, document reading/grepping/listing, and external doc sync."""

from __future__ import annotations

import httpx
import pytest

from app.knowledge import knowledge_manager as km
from app.knowledge.paths import folder_to_namespace, user_folder_key


class TestUserAndNamespacePaths:
    def test_user_folder_key_builds_scoped_path(self):
        assert user_folder_key("alice@example.com", "Tax 2026") == "users/alice/tax-2026"
        assert user_folder_key("user-123", "research/notes") == "users/user-123/research/notes"

    def test_user_folder_key_preserves_already_scoped_paths(self):
        assert user_folder_key("alice", "users/alice/docs") == "users/alice/docs"
        assert user_folder_key("alice", "projects/alpha") == "projects/alpha"
        assert user_folder_key("alice", "agents/writer") == "agents/writer"

    def test_folder_to_namespace(self):
        assert folder_to_namespace("contracts") == "kb-contracts"
        assert folder_to_namespace("users/alice/notes") == "kb-users-alice-notes"


class TestDocumentOperations:
    def test_list_read_and_grep_documents(self, workspace):
        km.save_text_file(
            "docs/architecture",
            "overview.md",
            "# System Overview\nThe coordinator routes tasks to Platform Docs.\n",
            auto_index=False,
        )
        km.save_text_file(
            "docs/architecture",
            "runbook.txt",
            "Step 1: Check health endpoint.\nStep 2: Sync folders.\n",
            auto_index=False,
        )

        listing = km.list_folder_documents("docs/architecture")
        assert listing["folder"] == "docs/architecture"
        filenames = [d["filename"] for d in listing["documents"]]
        assert filenames == ["overview.md", "runbook.txt"]

        page = km.read_folder_document("docs/architecture", "overview.md", offset=0, max_chars=25)
        assert page["filename"] == "overview.md"
        assert page["content"].startswith("# System Overview")
        assert page["has_more"] is True
        assert page["next_offset"] == 25

        grep_res = km.grep_folder_documents("Platform Docs", folder="docs/architecture")
        assert grep_res["total_matches"] == 1
        assert grep_res["matches"][0]["filename"] == "overview.md"
        assert grep_res["matches"][0]["line"] == 2

    def test_sync_external_docs_from_llms_txt(self, workspace):
        llms_txt = (
            "# Product Docs\n\n"
            "- [Getting Started](https://docs.example.com/getting-started.md): Quickstart guide\n"
            "- [API Reference](https://docs.example.com/api.md): Endpoints\n"
        )
        pages = {
            "https://docs.example.com/llms.txt": llms_txt,
            "https://docs.example.com/getting-started.md": "# Getting Started\nWelcome to the platform.\n",
            "https://docs.example.com/api.md": "# API Reference\nGET /v1/models\n",
        }

        def handler(request: httpx.Request) -> httpx.Response:
            body = pages.get(str(request.url))
            if body is None:
                return httpx.Response(404, text="not found")
            return httpx.Response(200, text=body)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        report = km.sync_external_docs(
            "docs/example",
            "https://docs.example.com/llms.txt",
            client=client,
        )
        assert report["status"] == "ok"
        assert sorted(report["synced_files"]) == ["api.md", "getting-started.md", "llms.txt"]

        read_back = km.read_folder_document("docs/example", "getting-started.md")
        assert "Welcome to the platform." in read_back["content"]


class TestPlatformDocsAgentAndTools:
    def test_platform_docs_tools_surface(self, workspace):
        from app.tools import platform_tools as tools

        km.save_text_file(
            "reference/guides",
            "deployment.md",
            "# Deployment Guide\nUse make up to start all containers.\n",
            auto_index=False,
        )

        listed = tools.list_docs_pages("reference/guides")
        assert listed["status"] == "ok"
        assert listed["total_documents"] == 1

        grepped = tools.grep_docs_pages("make up", folder="reference/guides")
        assert grepped["status"] == "ok"
        assert grepped["total_matches"] == 1

        read_res = tools.read_doc_page("reference/guides", "deployment.md")
        assert read_res["status"] == "ok"
        assert "Use make up" in read_res["content"]

        searched = tools.search_docs_knowledge("make up", folder="reference/guides")
        assert searched["status"] == "ok"

    def test_platform_docs_is_registered_core_agent(self):
        from app.agents import runtime, store

        assert "platform-docs" in store.PLATFORM_AGENT_IDS
        assert "platform-docs" in store.RESERVED_AGENT_IDS
        assert "platform-docs" in runtime.CORE_AGENT_IDS

