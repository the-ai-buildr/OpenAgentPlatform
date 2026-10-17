"""Toolkit assembly and graceful fallback for the `project-researcher` agent.

Toolkits are constructed but never invoked, so these tests make no network calls.
Missing optional dependencies are simulated by blocking their imports.
"""

from __future__ import annotations

import sys

import pytest

from app.tools import research_tools as rt


@pytest.fixture()
def no_agentql_key(monkeypatch):
    monkeypatch.setattr(rt.settings, "agentql_api_key", "")
    monkeypatch.delenv("AGENTQL_API_KEY", raising=False)


def toolkit_names(toolkits) -> list[str]:
    return [getattr(t, "__name__", None) or type(t).__name__ for t in toolkits]


class TestAssembly:
    def test_builds_the_four_core_toolkits_plus_inspector(self, workspace, no_agentql_key):
        toolkits = rt.build_researcher_toolkits()
        assert toolkit_names(toolkits) == [
            "DuckDuckGoTools",
            "ArxivTools",
            "Newspaper4kTools",
            "WebsiteTools",
            "inspect_research_toolkits",
        ]

    def test_aliases_point_at_the_builder(self):
        assert rt._build_researcher_tools is rt.build_researcher_toolkits
        assert rt.build_researcher_tools is rt.build_researcher_toolkits
        assert [rt.inspect_research_toolkits] == rt.RESEARCH_TOOLS

    def test_arxiv_downloads_go_under_the_workspace(self, workspace, no_agentql_key):
        status = rt.describe_researcher_toolkits()
        expected = workspace / "research" / "arxiv"
        assert status["arxiv_download_dir"] == str(expected)
        assert expected.is_dir()

    def test_unwritable_workspace_falls_back_to_a_temp_dir(self, monkeypatch, tmp_path):
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("file, not folder")
        monkeypatch.setattr(rt.app_settings, "WORKSPACE_ROOT", blocker)
        fallback = rt._resolve_arxiv_download_dir()
        assert fallback.name == "oap_arxiv"
        assert fallback.is_dir()

    def test_status_report(self, workspace, no_agentql_key):
        status = rt.inspect_research_toolkits()
        assert status["status"] == "ok"
        assert status["active_toolkits"] == ["duckduckgo", "arxiv", "newspaper4k", "website"]
        assert status["duckduckgo"] and status["arxiv"] and status["newspaper4k"] and status["website"]
        assert status["agentql"] is False
        assert status["agentql_api_key_configured"] is False
        assert "AGENTQL_API_KEY" in status["toolkits"]["agentql"]["note"]


class TestGracefulFallback:
    @pytest.mark.parametrize(
        ("module", "key", "toolkit"),
        [
            ("agno.tools.duckduckgo", "duckduckgo", "DuckDuckGoTools"),
            ("agno.tools.arxiv", "arxiv", "ArxivTools"),
            ("agno.tools.newspaper4k", "newspaper4k", "Newspaper4kTools"),
            ("agno.tools.website", "website", "WebsiteTools"),
        ],
    )
    def test_missing_dependency_disables_only_that_toolkit(
        self, workspace, no_agentql_key, monkeypatch, module, key, toolkit
    ):
        monkeypatch.setitem(sys.modules, module, None)
        status = rt.describe_researcher_toolkits()
        assert status[key] is False
        assert "ImportError" in status["toolkits"][key]["error"] or "ModuleNotFoundError" in status[
            "toolkits"
        ][key]["error"]
        assert key not in status["active_toolkits"]
        assert len(status["active_toolkits"]) == 3

        names = toolkit_names(rt.build_researcher_toolkits())
        assert toolkit not in names
        assert names[-1] == "inspect_research_toolkits"

    def test_every_dependency_missing_still_returns_the_inspector(
        self, workspace, no_agentql_key, monkeypatch
    ):
        for module in ("agno.tools.duckduckgo", "agno.tools.arxiv", "agno.tools.newspaper4k", "agno.tools.website"):
            monkeypatch.setitem(sys.modules, module, None)
        assert toolkit_names(rt.build_researcher_toolkits()) == ["inspect_research_toolkits"]
        assert rt.describe_researcher_toolkits()["active_toolkits"] == []

    def test_agentql_key_without_package_reports_why(self, workspace, monkeypatch):
        monkeypatch.setattr(rt.settings, "agentql_api_key", "aql-test-key")
        monkeypatch.setitem(sys.modules, "agentql", None)
        status = rt.describe_researcher_toolkits()
        assert status["agentql"] is False
        assert status["agentql_api_key_configured"] is True
        assert status["toolkits"]["agentql"]["api_key_configured"] is True
        assert "agentql" in status["toolkits"]["agentql"]["note"]
        assert "AgentQLTools" not in toolkit_names(rt.build_researcher_toolkits())

    def test_agentql_key_from_environment_is_detected(self, monkeypatch):
        monkeypatch.setattr(rt.settings, "agentql_api_key", "")
        monkeypatch.setenv("AGENTQL_API_KEY", "  env-key  ")
        assert rt._configured_agentql_api_key() == "env-key"
