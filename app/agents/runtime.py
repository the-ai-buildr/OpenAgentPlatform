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

from app import catalog_loader
from app.agents import store
from app.knowledge import knowledge_manager as km
from app.settings import settings
from app.tools import phoenix_tools
from app.tools import platform_tools as tools

logger = logging.getLogger(__name__)

CORE_AGENT_IDS = (
    "platform-builder",
    "platform-engineer",
    "platform-manager",
    "platform-telemetry",
)
TEAM_ID = "oap"
TEAM_NAME = "Platform Coordinator"

_lock = threading.RLock()
_core_agents: dict[str, Any] = {}
_dynamic_agents: dict[str, Any] = {}
_team: Any = None
_catalog_teams: dict[str, Any] = {}
_catalog_workflows: dict[str, Any] = {}
_db: Any = None
_registry: Any = None
_on_change: list[Callable[[], None]] = []


def get_db() -> Any:
    """Shared platform database. Built lazily so importing this module is cheap."""
    global _db
    if _db is None:
        from agno.db.postgres import PostgresDb

        _db = PostgresDb(id="oap-runtime", db_url=settings.database_url)
    return _db


def build_model(model_id: str | None = None) -> Any:
    """OpenRouter chat model. Raises when no API key is configured."""
    from agno.models.openrouter import OpenRouter

    if not settings.llm_configured:
        raise RuntimeError(
            "OPENROUTER_API_KEY is not set. Add it to .env and restart the API container."
        )
    return OpenRouter(
        id=model_id or settings.oap_default_model,
        api_key=settings.openrouter_api_key,
        base_url=settings.openrouter_base_url,
        max_tokens=settings.oap_model_max_tokens,
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
        model=build_model(settings.oap_builder_model),
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
        role="Explains and debugs the OAP codebase",
        model=build_model(settings.oap_engineer_model),
        tools=[
            *tools.SOURCE_TOOLS,
            tools.list_registered_agents,
            tools.list_workspace_directories_and_files,
            tools.inspect_file_watcher_status,
        ],
        instructions=[
            "You answer questions about how this OAP deployment is implemented.",
            "Read source files before describing behaviour; never guess at code you have not read.",
            "You cannot read configuration or secret files - say so rather than speculating.",
        ],
        **common,
    )

    manager = Agent(
        id="platform-manager",
        name="Platform Manager",
        role="Operates the platform: health, routing and knowledge hygiene",
        model=build_model(settings.oap_manager_model),
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

    telemetry = Agent(
        id="platform-telemetry",
        name="Platform Telemetry",
        role="Reads Phoenix traces and spans exported by this OAP",
        model=build_model(settings.oap_manager_model),
        tools=[
            phoenix_tools.list_phoenix_projects,
            phoenix_tools.list_recent_traces,
            phoenix_tools.list_recent_spans,
            tools.inspect_platform_health,
        ],
        instructions=[
            "You explain what the running platform actually did, using Phoenix traces.",
            "List projects before querying traces or spans. The default project is named default.",
            "Quote span names, status codes and trace ids from the tool results.",
            "Do not invent spans, latencies or errors that the tools did not return.",
            "If Phoenix is unreachable, say so and include the error from the tool.",
        ],
        **common,
    )

    return {agent.id: agent for agent in (builder, engineer, manager, telemetry)}


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
        name=TEAM_NAME,
        members=members,
        model=build_model(settings.oap_manager_model),
        db=get_db(),
        instructions=[
            "Route platform work to the right specialist.",
            "Agent and folder creation goes to Platform Builder.",
            "Questions about the codebase go to Platform Engineer.",
            "Health, routing and indexing go to Platform Manager.",
            "Traces, spans, latency and errors go to Platform Telemetry.",
            "Answer directly only when no member is a better fit.",
        ],
        add_history_to_context=True,
        num_history_runs=10,
        markdown=True,
    )


def _build_catalog_teams(agents: dict[str, Any]) -> dict[str, Any]:
    from agno.team import Team

    teams: dict[str, Any] = {}
    for spec in catalog_loader.team_files():
        missing = [member for member in spec.members if member not in agents]
        if missing or not spec.members:
            logger.warning("Catalog team %s skipped; missing members %s", spec.team_id, missing)
            continue
        teams[spec.team_id] = Team(
            id=spec.team_id,
            name=spec.name,
            members=[agents[member] for member in spec.members],
            model=build_model(settings.oap_manager_model),
            db=get_db(),
            instructions=[spec.instructions] if spec.instructions else None,
            markdown=True,
        )
    return teams


def _build_catalog_workflows(agents: dict[str, Any]) -> dict[str, Any]:
    from agno.workflow.step import Step
    from agno.workflow.workflow import Workflow

    workflows: dict[str, Any] = {}
    for spec in catalog_loader.workflow_files():
        agent = agents.get(spec.agent_id)
        if agent is None:
            logger.warning(
                "Catalog workflow %s skipped; agent %s is not loaded",
                spec.workflow_id,
                spec.agent_id,
            )
            continue
        workflows[spec.workflow_id] = Workflow(
            id=spec.workflow_id,
            name=spec.name,
            description=spec.description or None,
            steps=[Step(name=spec.name, agent=agent)],
            db=get_db(),
        )
    return workflows


def _install_catalog(agents: dict[str, Any]) -> None:
    global _catalog_teams, _catalog_workflows
    _catalog_teams = _build_catalog_teams(agents)
    _catalog_workflows = _build_catalog_workflows(agents)


def initialize() -> None:
    """Build core agents, the team and all registered custom agents."""
    global _team
    catalog_loader.refresh(notify=False)
    with _lock:
        _core_agents.clear()
        _core_agents.update(_build_core_agents())
        _dynamic_agents.clear()
        _dynamic_agents.update(_build_dynamic_agents())
        _install_catalog({**_core_agents, **_dynamic_agents})
        _team = _build_team(list(_core_agents.values()))


def reload_dynamic_agents() -> dict[str, Any]:
    """Rebuild custom agents from the registry and notify the platform."""
    catalog_loader.refresh(notify=False)
    if not settings.llm_configured:
        return {}
    with _lock:
        if not _core_agents:
            initialize()
        else:
            _dynamic_agents.clear()
            _dynamic_agents.update(_build_dynamic_agents())
            _install_catalog({**_core_agents, **_dynamic_agents})
    for callback in list(_on_change):
        try:
            callback()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Agent reload listener failed: %s", exc)
    return dict(_dynamic_agents)


def on_agents_changed(callback: Callable[[], None]) -> None:
    """Register a listener fired after the agent set is rebuilt (platform resync)."""
    if callback not in _on_change:
        _on_change.append(callback)


def get_catalog_teams() -> list[Any]:
    if not _core_agents and settings.llm_configured:
        initialize()
    return list(_catalog_teams.values())


def get_catalog_workflows() -> list[Any]:
    if not _core_agents and settings.llm_configured:
        initialize()
    return list(_catalog_workflows.values())


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


def get_registry() -> Any:
    """Models and tools the control-plane Studio can wire into a new agent.

    Built once and handed to the platform. Studio writes published agents into the
    same Postgres database, and this registry is what rehydrates their model
    and tool references when Open WebUI runs them.
    """
    global _registry
    if _registry is None:
        _registry = _build_registry()
    return _registry


def _build_registry() -> Any:
    from agno.registry import Registry
    from agno.tools.calculator import CalculatorTools

    model_ids: list[str] = []
    for model_id in (
        settings.oap_default_model,
        settings.oap_builder_model,
        settings.oap_engineer_model,
        settings.oap_manager_model,
    ):
        if model_id and model_id not in model_ids:
            model_ids.append(model_id)

    models = []
    for model_id in model_ids:
        model = build_model(model_id)
        model.name = model_id
        models.append(model)

    tools: list[Any] = [CalculatorTools()]
    try:
        from agno.tools.duckduckgo import DuckDuckGoTools

        tools.append(DuckDuckGoTools())
    except Exception as exc:  # pragma: no cover - optional toolkit
        logger.warning("Web search tool not registered for Studio: %s", exc)

    return Registry(
        name="Open Agent Platform",
        description="Models and tools available in the local control plane.",
        models=models,
        tools=tools,
        dbs=[get_db()],
    )


def list_published_studio_agents(exclude: set[str]) -> list[dict[str, str]]:
    """Agents published from the control plane, excluding ones defined in code."""
    from agno.db.base import ComponentType

    try:
        rows, _total = get_db().list_components(component_type=ComponentType.AGENT, limit=200)
    except Exception as exc:
        logger.warning("Studio catalogue unavailable: %s", exc)
        return []

    published: list[dict[str, str]] = []
    for row in rows:
        agent_id = str(row.get("component_id") or "")
        if not agent_id or agent_id in exclude or row.get("current_version") is None:
            continue
        published.append(
            {
                "id": agent_id,
                "name": str(row.get("name") or agent_id),
                "kind": "studio",
            }
        )
    return published


def load_published_studio_agent(agent_id: str) -> Any | None:
    """Rehydrate one published Studio agent so the bridge can run it."""
    from agno.os.utils import get_agent_by_id

    try:
        return get_agent_by_id(
            agent_id=agent_id,
            agents=get_all_agents_list(),
            db=get_db(),
            registry=get_registry(),
            create_fresh=True,
            published_only=True,
            strict=False,
        )
    except Exception as exc:
        logger.warning("Studio agent %s unavailable: %s", agent_id, exc)
        return None


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
    if model_id in agents:
        return agents[model_id]
    if model_id in _catalog_teams:
        return _catalog_teams[model_id]
    if model_id in _catalog_workflows:
        return _catalog_workflows[model_id]
    studio = load_published_studio_agent(model_id)
    if studio is not None:
        return studio
    return get_team()


def describe_agents() -> list[dict[str, str]]:
    """Catalogue used by the `/v1/models` bridge endpoint.

    Empty without an LLM key: advertising models that cannot answer would make
    Open WebUI show a picker full of options that all fail.
    """
    if not settings.llm_configured:
        return []
    agents = get_all_agents_dict()
    entries = [
        {
            "id": TEAM_ID,
            "name": TEAM_NAME,
            "kind": "team",
            "description": "Routes platform work to the right specialist agent",
        },
    ]
    entries.extend(
        {
            "id": agent_id,
            "name": getattr(agent, "name", agent_id) or agent_id,
            "kind": "agent",
            "description": str(
                getattr(agent, "role", None) or getattr(agent, "description", None) or ""
            ),
        }
        for agent_id, agent in agents.items()
    )
    entries.extend(
        {
            "id": team_id,
            "name": getattr(team, "name", team_id) or team_id,
            "kind": "team",
            "description": str(
                getattr(team, "description", None) or getattr(team, "role", None) or ""
            ),
        }
        for team_id, team in _catalog_teams.items()
    )
    entries.extend(
        {
            "id": workflow_id,
            "name": getattr(workflow, "name", workflow_id) or workflow_id,
            "kind": "workflow",
            "description": str(getattr(workflow, "description", None) or ""),
        }
        for workflow_id, workflow in _catalog_workflows.items()
    )
    entries.extend(list_published_studio_agents({entry["id"] for entry in entries}))
    return entries


# Any registry mutation (from a tool call or a direct edit) rebuilds the
# dynamic agents without `store` ever importing this module.
store.subscribe(lambda: reload_dynamic_agents())
