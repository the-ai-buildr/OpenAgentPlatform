"""Builds the live Agno agents and the coordinating team.

`runtime` is the only module that constructs Agno objects. It reads specs from
`app.agents.store` and tool callables from `app.tools.platform_tools`; neither
of those imports `runtime`, so the dependency graph stays acyclic.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from app.agents import store
from app.knowledge import knowledge_manager as km
from app.settings import settings
from app.tools import platform_tools as tools

logger = logging.getLogger(__name__)

CORE_AGENT_IDS = ("platform-builder", "platform-engineer", "platform-manager")
TEAM_ID = "agno"

_lock = threading.RLock()
_core_agents: dict[str, Any] = {}
_dynamic_agents: dict[str, Any] = {}
_team: Any = None
_db: Any = None
_on_change: list[Callable[[], None]] = []


def get_db() -> Any:
    """Shared AgentOS database. Built lazily so importing this module is cheap."""
    global _db
    if _db is None:
        from agno.db.postgres import PostgresDb

        _db = PostgresDb(id="agentos-runtime", db_url=settings.database_url)
    return _db


def build_model(model_id: str | None = None) -> Any:
    """OpenRouter chat model. Raises when no API key is configured."""
    from agno.models.openrouter import OpenRouter

    if not settings.llm_configured:
        raise RuntimeError(
            "OPENROUTER_API_KEY is not set. Add it to .env and restart the API container."
        )
    return OpenRouter(
        id=model_id or settings.agno_default_model,
        api_key=settings.openrouter_api_key,
        base_url=settings.openrouter_base_url,
        max_tokens=settings.agno_model_max_tokens,
    )


def _folder_knowledge(folder: str | None) -> Any | None:
    if not folder or not km.embeddings_available():
        return None
    try:
        return km.get_or_create_folder_knowledge(folder)
    except Exception as exc:
        logger.warning("Knowledge base for folder %r unavailable: %s", folder, exc)
        return None


def _build_core_agents() -> dict[str, Any]:
    from agno.agent import Agent

    db = get_db()
    common = {
        "db": db,
        "add_history_to_context": True,
        "num_history_runs": 10,
        "markdown": True,
    }

    builder = Agent(
        id="platform-builder",
        name="Platform Builder",
        role="Creates and configures specialist agents and their knowledge folders",
        model=build_model(settings.agno_builder_model),
        tools=[*tools.REGISTRY_TOOLS, *tools.WORKSPACE_TOOLS],
        instructions=[
            "You build and maintain the specialist agents on this platform.",
            "Create a workspace folder before binding it to a new agent.",
            "Do not attempt to modify the core platform agents; report the restriction instead.",
            "Do not re-index a folder unless its files changed - the watcher handles that.",
        ],
        **common,
    )

    engineer = Agent(
        id="platform-engineer",
        name="Platform Engineer",
        role="Explains and debugs the AgentOS codebase",
        model=build_model(settings.agno_engineer_model),
        tools=[
            *tools.SOURCE_TOOLS,
            tools.list_registered_agents,
            tools.list_workspace_directories_and_files,
            tools.inspect_file_watcher_status,
        ],
        instructions=[
            "You answer questions about how this AgentOS deployment is implemented.",
            "Read source files before describing behaviour; never guess at code you have not read.",
            "You cannot read configuration or secret files - say so rather than speculating.",
        ],
        **common,
    )

    manager = Agent(
        id="platform-manager",
        name="Platform Manager",
        role="Operates the platform: health, routing and knowledge hygiene",
        model=build_model(settings.agno_manager_model),
        tools=[
            *tools.HEALTH_TOOLS,
            tools.create_workspace_directory,
            tools.list_workspace_directories_and_files,
            tools.route_openwebui_upload_to_folder,
            tools.sync_folder_to_pgvector,
        ],
        instructions=[
            "You keep the platform healthy and route documents to the right folders.",
            "Run the health check before claiming a component is broken.",
            "If embeddings are not configured, say that indexing is disabled rather than retrying.",
        ],
        **common,
    )

    return {agent.id: agent for agent in (builder, engineer, manager)}


def _build_dynamic_agents() -> dict[str, Any]:
    from agno.agent import Agent

    agents: dict[str, Any] = {}
    for spec in store.load_specs():
        agent_tools: list[Any] = []
        if spec.enable_web_search:
            try:
                from agno.tools.duckduckgo import DuckDuckGoTools

                agent_tools.append(DuckDuckGoTools())
            except Exception as exc:  # pragma: no cover - optional dependency
                logger.warning("Web search unavailable for %s: %s", spec.agent_id, exc)

        knowledge = _folder_knowledge(spec.knowledge_folder)
        try:
            agents[spec.agent_id] = Agent(
                id=spec.agent_id,
                name=spec.name,
                role=spec.role or None,
                model=build_model(spec.model_id),
                db=get_db(),
                tools=agent_tools or None,
                knowledge=knowledge,
                search_knowledge=knowledge is not None,
                instructions=spec.instructions or None,
                add_history_to_context=True,
                num_history_runs=10,
                markdown=True,
            )
        except Exception as exc:
            logger.error("Could not build custom agent %s: %s", spec.agent_id, exc)
    return agents


def _build_team(members: list[Any]) -> Any:
    from agno.team import Team

    return Team(
        id=TEAM_ID,
        name="Agno Platform Coordinator",
        members=members,
        model=build_model(settings.agno_manager_model),
        db=get_db(),
        instructions=[
            "Route platform work to the right specialist.",
            "Agent and folder creation goes to Platform Builder.",
            "Questions about the codebase go to Platform Engineer.",
            "Health, routing and indexing go to Platform Manager.",
            "Answer directly only when no member is a better fit.",
        ],
        add_history_to_context=True,
        num_history_runs=10,
        markdown=True,
    )


def initialize() -> None:
    """Build core agents, the team and all registered custom agents."""
    global _team
    with _lock:
        _core_agents.clear()
        _core_agents.update(_build_core_agents())
        _dynamic_agents.clear()
        _dynamic_agents.update(_build_dynamic_agents())
        _team = _build_team(list(_core_agents.values()))


def reload_dynamic_agents() -> dict[str, Any]:
    """Rebuild custom agents from the registry and notify AgentOS."""
    with _lock:
        if not _core_agents:
            initialize()
        else:
            _dynamic_agents.clear()
            _dynamic_agents.update(_build_dynamic_agents())
    for callback in list(_on_change):
        try:
            callback()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Agent reload listener failed: %s", exc)
    return dict(_dynamic_agents)


def on_agents_changed(callback: Callable[[], None]) -> None:
    """Register a listener fired after the agent set is rebuilt (AgentOS resync)."""
    if callback not in _on_change:
        _on_change.append(callback)


def get_team() -> Any:
    if _team is None:
        initialize()
    return _team


def get_all_agents_dict() -> dict[str, Any]:
    if not _core_agents:
        initialize()
    return {**_core_agents, **_dynamic_agents}


def get_all_agents_list() -> list[Any]:
    return list(get_all_agents_dict().values())


def get_runner(model_id: str | None) -> Any:
    """Resolve an Open WebUI model id to a team or agent, defaulting to the team.

    Returns None when no runnable exists — including the degraded case where no
    LLM key is configured, so nothing can be constructed at all.
    """
    if not settings.llm_configured:
        return None
    if not model_id or model_id == TEAM_ID:
        return get_team()
    agents = get_all_agents_dict()
    return agents.get(model_id) or get_team()


def describe_agents() -> list[dict[str, str]]:
    """Catalogue used by the `/v1/models` bridge endpoint.

    Empty without an LLM key: advertising models that cannot answer would make
    Open WebUI show a picker full of options that all fail.
    """
    if not settings.llm_configured:
        return []
    entries = [
        {"id": TEAM_ID, "name": "Agno Platform Coordinator", "kind": "team"},
    ]
    entries.extend(
        {
            "id": agent_id,
            "name": getattr(agent, "name", agent_id) or agent_id,
            "kind": "agent",
        }
        for agent_id, agent in get_all_agents_dict().items()
    )
    return entries


# Any registry mutation (from a tool call or a direct edit) rebuilds the
# dynamic agents without `store` ever importing this module.
store.subscribe(lambda: reload_dynamic_agents())
