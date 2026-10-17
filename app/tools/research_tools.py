"""Research toolkits for the `project-researcher` specialist agent.

Provides web search (DuckDuckGo), academic paper search and PDF reading (arXiv),
full-text news article extraction (Newspaper4k), general webpage reading
(WebsiteTools), and optional structured web extraction (AgentQL).

Every toolkit import and instantiation is wrapped in `try/except Exception` so
missing optional dependencies or unset API keys never crash agent construction.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from app import settings as app_settings
from app.settings import settings

logger = logging.getLogger(__name__)


def _resolve_arxiv_download_dir() -> Path:
    """Return a writable directory for arXiv PDF downloads.

    Prefers `WORKSPACE_ROOT / "research" / "arxiv"`, falling back to a temporary
    directory when `/app/workspace_files` is not writable (for example in local
    unit tests running outside Docker).
    """
    preferred = app_settings.WORKSPACE_ROOT / "research" / "arxiv"
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        return preferred
    except OSError:
        fallback = Path(tempfile.gettempdir()) / "oap_arxiv"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def _configured_agentql_api_key() -> str:
    """Return the configured AgentQL API key, if any."""
    return (settings.agentql_api_key or os.environ.get("AGENTQL_API_KEY", "")).strip()


def _assemble_researcher_toolkits() -> tuple[list[Any], dict[str, dict[str, Any]], Path]:
    """Instantiate available research toolkits and record status metadata."""
    toolkits: list[Any] = []
    statuses: dict[str, dict[str, Any]] = {}
    arxiv_dir = _resolve_arxiv_download_dir()

    # 1. DuckDuckGo web & news search
    try:
        from agno.tools.duckduckgo import DuckDuckGoTools

        ddg = DuckDuckGoTools(enable_search=True, enable_news=True)
        toolkits.append(ddg)
        statuses["duckduckgo"] = {
            "enabled": True,
            "toolkit": "DuckDuckGoTools",
            "features": ["web_search", "news_search"],
        }
    except Exception as exc:
        logger.warning("DuckDuckGoTools unavailable for project-researcher: %s", exc)
        statuses["duckduckgo"] = {
            "enabled": False,
            "toolkit": "DuckDuckGoTools",
            "error": f"{type(exc).__name__}: {exc}",
        }

    # 2. arXiv paper search & PDF reading
    try:
        from agno.tools.arxiv import ArxivTools

        arxiv_tools = ArxivTools(
            enable_search_arxiv=True,
            enable_read_arxiv_papers=True,
            download_dir=arxiv_dir,
        )
        toolkits.append(arxiv_tools)
        statuses["arxiv"] = {
            "enabled": True,
            "toolkit": "ArxivTools",
            "download_dir": str(arxiv_dir),
            "features": ["search_arxiv_and_return_articles", "read_arxiv_papers"],
        }
    except Exception as exc:
        logger.warning("ArxivTools unavailable for project-researcher: %s", exc)
        statuses["arxiv"] = {
            "enabled": False,
            "toolkit": "ArxivTools",
            "download_dir": str(arxiv_dir),
            "error": f"{type(exc).__name__}: {exc}",
        }

    # 3. Newspaper4k full-text article extraction
    try:
        from agno.tools.newspaper4k import Newspaper4kTools

        newspaper_tools = Newspaper4kTools(enable_read_article=True, include_summary=True)
        toolkits.append(newspaper_tools)
        statuses["newspaper4k"] = {
            "enabled": True,
            "toolkit": "Newspaper4kTools",
            "features": ["read_article", "include_summary"],
        }
    except Exception as exc:
        logger.warning("Newspaper4kTools unavailable for project-researcher: %s", exc)
        statuses["newspaper4k"] = {
            "enabled": False,
            "toolkit": "Newspaper4kTools",
            "error": f"{type(exc).__name__}: {exc}",
        }

    # 4. WebsiteTools general URL reader
    try:
        from agno.tools.website import WebsiteTools

        website_tools = WebsiteTools()
        toolkits.append(website_tools)
        statuses["website"] = {
            "enabled": True,
            "toolkit": "WebsiteTools",
            "features": ["read_url"],
        }
    except Exception as exc:
        logger.warning("WebsiteTools unavailable for project-researcher: %s", exc)
        statuses["website"] = {
            "enabled": False,
            "toolkit": "WebsiteTools",
            "error": f"{type(exc).__name__}: {exc}",
        }

    # 5. Optional AgentQL structured extraction (only when AGENTQL_API_KEY is set)
    agentql_key = _configured_agentql_api_key()
    if agentql_key:
        try:
            import agentql  # noqa: F401
            from agno.tools.agentql import AgentQLTools

            agentql_tools = AgentQLTools(api_key=agentql_key)
            toolkits.append(agentql_tools)
            statuses["agentql"] = {
                "enabled": True,
                "toolkit": "AgentQLTools",
                "api_key_configured": True,
            }
        except Exception as exc:
            logger.warning("AgentQLTools unavailable for project-researcher: %s", exc)
            statuses["agentql"] = {
                "enabled": False,
                "toolkit": "AgentQLTools",
                "api_key_configured": True,
                "error": f"{type(exc).__name__}: {exc}",
                "note": (
                    "Install the optional `agentql` package and Playwright dependencies "
                    "to enable AgentQL."
                ),
            }
    else:
        statuses["agentql"] = {
            "enabled": False,
            "toolkit": "AgentQLTools",
            "api_key_configured": False,
            "note": (
                "Set AGENTQL_API_KEY in .env (and ensure `agentql` is installed) "
                "to enable AgentQL structured scraping."
            ),
        }

    return toolkits, statuses, arxiv_dir


def describe_researcher_toolkits() -> dict[str, Any]:
    """Return structured status metadata for all `project-researcher` toolkits."""
    _, statuses, arxiv_dir = _assemble_researcher_toolkits()
    active = [name for name, info in statuses.items() if info.get("enabled")]
    agentql_configured = bool(_configured_agentql_api_key())
    return {
        "status": "ok",
        "active_toolkits": active,
        "duckduckgo": bool(statuses["duckduckgo"].get("enabled")),
        "arxiv": bool(statuses["arxiv"].get("enabled")),
        "newspaper4k": bool(statuses["newspaper4k"].get("enabled")),
        "website": bool(statuses["website"].get("enabled")),
        "agentql": bool(statuses["agentql"].get("enabled")),
        "agentql_api_key_configured": agentql_configured,
        "arxiv_download_dir": str(arxiv_dir),
        "toolkits": statuses,
    }


def inspect_research_toolkits() -> dict[str, Any]:
    """Report which research toolkits are active and whether `AGENTQL_API_KEY` is configured.

    Covers `duckduckgo`, `arxiv`, `newspaper4k`, `website`, and `agentql`.
    """
    return describe_researcher_toolkits()


def build_researcher_toolkits() -> list[Any]:
    """Instantiate and return the research toolkits and inspection tool for `project-researcher`."""
    toolkits, _, _ = _assemble_researcher_toolkits()
    toolkits.append(inspect_research_toolkits)
    return toolkits


# Compatibility aliases used across runtime/tests
_build_researcher_tools = build_researcher_toolkits
build_researcher_tools = build_researcher_toolkits

RESEARCH_TOOLS = [inspect_research_toolkits]
