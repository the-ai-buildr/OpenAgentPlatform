"""Builds the live Agno agents and teams.

`runtime` is the only module that constructs Agno objects. It reads specs from
`app.agents.store` and tool callables from `app.tools`; none of those import
`runtime` at module level, so the dependency graph stays acyclic.

Two groups are built here:

- Platform: the `platform-*` agents, plus the `oap` coordinator team built from
  its own copies of them.
- Project: `project-dev` (a team with four specialist subagents),
  `project-researcher`, and one team per user project (`store.ProjectSpec`).
  None of them are members of `oap`.

Agno's `Team` binds each member to itself on initialisation (`team_id`,
`parent_team_id`, its result store), so an instance must never belong to more
than one team. Every team therefore gets freshly built members from the
factories below instead of sharing the standalone instances.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any, NamedTuple

from app import catalog_loader
from app.agents import store
from app.knowledge import knowledge_manager as km
from app.settings import settings
from app.skills_manager import build_skills
from app.tools import phoenix_tools, research_tools, scheduler_tools, skill_tools
from app.tools import platform_tools as tools

logger = logging.getLogger(__name__)

CORE_AGENT_IDS = (
    "platform-builder",
    "platform-engineer",
    "platform-manager",
    "platform-telemetry",
    "platform-skills",
    "platform-scheduler",
)
TEAM_ID = "oap"
TEAM_NAME = "Platform Coordinator"

PROJECT_DEV_ID = "project-dev"
PROJECT_RESEARCHER_ID = "project-researcher"
_PROJECT_TEMPLATE_KINDS = {PROJECT_DEV_ID: "team", PROJECT_RESEARCHER_ID: "agent"}
RESEARCHER_SKILLS = (
    "source-driven-development",
    "context-engineering",
    "doubt-driven-development",
)

_lock = threading.RLock()
_core_agents: dict[str, Any] = {}
_dynamic_agents: dict[str, Any] = {}
_project_templates: dict[str, Any] = {}
_project_teams: dict[str, Any] = {}
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


def _skills(names: list[str] | tuple[str, ...] | None = None) -> Any | None:
    """Skills for an agent. A broken workspace skill must not stop the platform booting."""
    try:
        return build_skills(list(names) if names is not None else None)
    except Exception as exc:
        logger.warning("Skills %s unavailable: %s", names or "(all)", exc)
        return None


def _common() -> dict[str, Any]:
    return {
        "db": get_db(),
        "add_history_to_context": True,
        "num_history_runs": 10,
        "markdown": True,
        "store_events": settings.store_run_events,
    }


# --------------------------------------------------------------------------- #
# Platform group
# --------------------------------------------------------------------------- #
def _build_platform_builder() -> Any:
    from agno.agent import Agent

    return Agent(
        id="platform-builder",
        name="Platform Builder",
        role="Creates and configures specialist agents, projects and their knowledge folders",
        model=build_model(settings.oap_builder_model),
        tools=[*tools.REGISTRY_TOOLS, *tools.WORKSPACE_TOOLS],
        instructions=[
            "You build and maintain the specialist agents and user projects on this platform.",
            "Create a workspace folder before binding it to a new agent.",
            "A project is a team: attach project-dev, project-researcher and custom agent ids "
            "as its members. Its folder defaults to projects/<project_id>.",
            "Do not attempt to modify the core platform agents; report the restriction instead.",
            "Deleting an agent or project needs human confirmation; say what it removes first.",
            "Do not re-index a folder unless its files changed - the watcher handles that.",
        ],
        **_common(),
    )


def _build_platform_engineer() -> Any:
    from agno.agent import Agent

    return Agent(
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
        **_common(),
    )


def _build_platform_manager() -> Any:
    from agno.agent import Agent

    return Agent(
        id="platform-manager",
        name="Platform Manager",
        role="Operates the platform: health, routing, knowledge hygiene and background runs",
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
            "Report background run status with check_background_run_status; it needs the "
            "agent or team id, the run id and, when known, the session id.",
        ],
        **_common(),
    )


def _build_platform_telemetry() -> Any:
    from agno.agent import Agent

    return Agent(
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
        **_common(),
    )


def _build_platform_skills() -> Any:
    from agno.agent import Agent

    return Agent(
        id="platform-skills",
        name="Platform Skills",
        role="Manages, validates, syncs and recommends the skills agents load",
        model=build_model(settings.oap_skills_model),
        tools=[
            *skill_tools.SKILL_TOOLS,
            tools.list_registered_agents,
            tools.create_or_update_agent,
        ],
        skills=_skills(),
        instructions=[
            "You manage agent skills: bundled ones in app/skills and workspace overrides in "
            "workspace_files/skills, which win over bundled skills of the same name.",
            "List or inspect skills before recommending or changing them; never invent names.",
            "To attach skills to a custom agent, read it with list_registered_agents first, then "
            "pass its existing role, instructions, model and folder to create_or_update_agent.",
            "Bundled skills cannot be deleted. Deleting a custom skill needs human confirmation.",
        ],
        **_common(),
    )


def _build_platform_scheduler() -> Any:
    from agno.agent import Agent

    return Agent(
        id="platform-scheduler",
        name="Platform Scheduler",
        role="Creates, inspects, triggers and manages cron schedules and their run history",
        model=build_model(settings.oap_scheduler_model),
        tools=[*scheduler_tools.SCHEDULER_TOOLS, tools.check_background_run_status],
        instructions=[
            "You manage cron schedules that call this platform's AgentOS endpoints.",
            "Run endpoints are /agents/<agent_id>/runs or /teams/<team_id>/runs, and the payload "
            "carries the prompt as message.",
            "List schedules before creating one so an existing one is updated, not duplicated.",
            "Cron expressions have five fields and use the schedule's timezone (UTC by default).",
            "Deleting a schedule needs human confirmation; to pause one, disable it instead.",
            "Use check_background_run_status to report on runs started in the background.",
        ],
        **_common(),
    )


_CORE_FACTORIES: dict[str, Callable[[], Any]] = {
    "platform-builder": _build_platform_builder,
    "platform-engineer": _build_platform_engineer,
    "platform-manager": _build_platform_manager,
    "platform-telemetry": _build_platform_telemetry,
    "platform-skills": _build_platform_skills,
    "platform-scheduler": _build_platform_scheduler,
}


def _build_core_agents() -> dict[str, Any]:
    return {agent_id: _CORE_FACTORIES[agent_id]() for agent_id in CORE_AGENT_IDS}


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
            "Agent, project and folder creation goes to Platform Builder.",
            "Questions about the codebase go to Platform Engineer.",
            "Health, routing, indexing and background run status go to Platform Manager.",
            "Traces, spans, latency and errors go to Platform Telemetry.",
            "Listing, recommending, syncing, writing or attaching skills goes to Platform Skills.",
            "Cron schedules and their run history go to Platform Scheduler.",
            "Software development and research are project work, not platform work: tell the user "
            "to pick project-dev or project-researcher in the model list, or to open a project "
            "that includes them. Platform Builder can create that project.",
            "Answer directly only when no member is a better fit.",
        ],
        add_history_to_context=True,
        num_history_runs=10,
        markdown=True,
        store_events=settings.store_run_events,
    )


# --------------------------------------------------------------------------- #
# Project group
# --------------------------------------------------------------------------- #
class _DevPersona(NamedTuple):
    agent_id: str
    name: str
    role: str
    skills: tuple[str, ...]
    instructions: tuple[str, ...]


DEV_PERSONAS: tuple[_DevPersona, ...] = (
    _DevPersona(
        agent_id="dev-code-reviewer",
        name="Dev Code Reviewer",
        role="Staff-level five-axis code review",
        skills=("code-review-and-quality", "code-simplification", "api-and-interface-design"),
        instructions=(
            "Review across correctness, readability, architecture, security and performance.",
            "Cite the file and line for every finding and order findings by severity.",
            "Propose the smallest fix for each issue; do not rewrite working code for style.",
        ),
    ),
    _DevPersona(
        agent_id="dev-test-engineer",
        name="Dev Test Engineer",
        role="Test strategy, coverage gaps and prove-it verification",
        skills=(
            "test-driven-development",
            "debugging-and-error-recovery",
            "browser-testing-with-devtools",
        ),
        instructions=(
            "Design tests that fail without the change and pass with it.",
            "For a bug, write the reproducing test before proposing a fix.",
            "Say what remains untested and the risk it carries.",
        ),
    ),
    _DevPersona(
        agent_id="dev-security-auditor",
        name="Dev Security Auditor",
        role="Security audits: OWASP Top 10, auth, secrets and trust boundaries",
        skills=("security-and-hardening", "deprecation-and-migration"),
        instructions=(
            "Audit input validation, authentication, authorization, secrets and trust boundaries.",
            "Tie each finding to an exploit path and a concrete fix.",
            "Flag deprecated or vulnerable dependencies together with a migration path.",
        ),
    ),
    _DevPersona(
        agent_id="dev-perf-auditor",
        name="Dev Perf Auditor",
        role="Performance, frontend and observability analysis",
        skills=(
            "performance-optimization",
            "frontend-ui-engineering",
            "observability-and-instrumentation",
        ),
        instructions=(
            "Find hot paths, N+1 queries, unbounded work and rendering bottlenecks.",
            "Ask for or propose a measurement before recommending an optimization.",
            "Name the traces, metrics or logs that would confirm the impact.",
        ),
    ),
)
_DEV_PERSONAS_BY_ID = {persona.agent_id: persona for persona in DEV_PERSONAS}


def _build_dev_subagent(persona: _DevPersona) -> Any:
    from agno.agent import Agent

    return Agent(
        id=persona.agent_id,
        name=persona.name,
        role=persona.role,
        model=build_model(settings.oap_dev_model),
        tools=list(tools.SOURCE_TOOLS),
        skills=_skills(persona.skills),
        instructions=[
            *persona.instructions,
            "Read code with the source tools before commenting on it; never review code you "
            "have not read.",
        ],
        db=get_db(),
        markdown=True,
        store_events=settings.store_run_events,
    )


def build_project_dev_team() -> Any:
    """A fresh `project-dev` team with its four specialist subagents."""
    from agno.team import Team
    from agno.team.mode import TeamMode

    return Team(
        id=PROJECT_DEV_ID,
        name="Project Dev",
        description="Development team that plans, builds and reviews code with four specialists",
        mode=TeamMode.coordinate,
        members=[_build_dev_subagent(persona) for persona in DEV_PERSONAS],
        model=build_model(settings.oap_dev_model),
        tools=[*tools.SOURCE_TOOLS, *tools.WORKSPACE_TOOLS],
        skills=_skills(),
        instructions=[
            "You lead a development team that plans, implements and reviews software changes.",
            "Pick the skill workflow that fits the request: spec, plan, implement in small "
            "increments, test, then review.",
            "Code review goes to Dev Code Reviewer, tests and debugging to Dev Test Engineer, "
            "security to Dev Security Auditor, performance and observability to Dev Perf Auditor.",
            "The source tools read this platform's own code under app/. For other code, work from "
            "what the user pastes or saves in a workspace folder.",
            "Merge member findings into one answer with the most severe issues first.",
        ],
        db=get_db(),
        add_history_to_context=True,
        num_history_runs=10,
        markdown=True,
        store_events=settings.store_run_events,
    )


def build_project_researcher(project_folder: str | None = None) -> Any:
    """A fresh `project-researcher`. Reports go to `project_folder` (default `research`)."""
    from agno.agent import Agent

    folder = project_folder or "research"
    return Agent(
        id=PROJECT_RESEARCHER_ID,
        name="Project Researcher",
        role="Researches the web, news and arXiv, then writes cited reports",
        model=build_model(settings.oap_researcher_model),
        tools=[
            *research_tools.build_researcher_toolkits(),
            tools.save_text_file_to_folder,
            tools.list_workspace_directories_and_files,
        ],
        skills=_skills(RESEARCHER_SKILLS),
        instructions=[
            "You research questions with web search, news, full articles and arXiv papers.",
            "Cite every claim with the URL or arXiv id it came from, and say when sources "
            "disagree or are thin.",
            f"Save finished reports as markdown in the '{folder}' workspace folder with "
            "save_text_file_to_folder.",
            "If a source type is missing, call inspect_research_toolkits and tell the user what "
            "is not configured.",
        ],
        **_common(),
    )


def _build_project_templates() -> dict[str, Any]:
    return {
        PROJECT_DEV_ID: build_project_dev_team(),
        PROJECT_RESEARCHER_ID: build_project_researcher(),
    }


# --------------------------------------------------------------------------- #
# Custom agents, user projects and catalog components
# --------------------------------------------------------------------------- #
def _build_custom_agent(spec: store.AgentSpec) -> Any | None:
    from agno.agent import Agent

    agent_tools: list[Any] = []
    if spec.enable_web_search:
        try:
            from agno.tools.duckduckgo import DuckDuckGoTools

            agent_tools.append(DuckDuckGoTools())
        except Exception as exc:  # pragma: no cover - optional dependency
            logger.warning("Web search unavailable for %s: %s", spec.agent_id, exc)

    knowledge = _folder_knowledge(spec.knowledge_folder)
    try:
        return Agent(
            id=spec.agent_id,
            name=spec.name,
            role=spec.role or None,
            model=build_model(spec.model_id),
            db=get_db(),
            tools=agent_tools or None,
            knowledge=knowledge,
            search_knowledge=knowledge is not None,
            skills=_skills(spec.skills) if spec.skills else None,
            instructions=spec.instructions or None,
            add_history_to_context=True,
            num_history_runs=10,
            markdown=True,
        )
    except Exception as exc:
        logger.error("Could not build custom agent %s: %s", spec.agent_id, exc)
        return None


def _build_dynamic_agents(specs: dict[str, store.AgentSpec]) -> dict[str, Any]:
    agents: dict[str, Any] = {}
    for agent_id, spec in specs.items():
        agent = _build_custom_agent(spec)
        if agent is not None:
            agents[agent_id] = agent
    return agents


def _is_known_member(
    member_id: str, specs: dict[str, store.AgentSpec], *, allow_platform: bool
) -> bool:
    return (
        member_id in _PROJECT_TEMPLATE_KINDS
        or member_id in _DEV_PERSONAS_BY_ID
        or member_id in specs
        or (allow_platform and member_id in _CORE_FACTORIES)
    )


def _build_member(
    member_id: str,
    specs: dict[str, store.AgentSpec],
    *,
    allow_platform: bool,
    project_folder: str | None = None,
) -> Any | None:
    """A fresh member instance for one team, or None when the id cannot be resolved."""
    if member_id == PROJECT_DEV_ID:
        return build_project_dev_team()
    if member_id == PROJECT_RESEARCHER_ID:
        return build_project_researcher(project_folder)
    if member_id in _DEV_PERSONAS_BY_ID:
        return _build_dev_subagent(_DEV_PERSONAS_BY_ID[member_id])
    if allow_platform and member_id in _CORE_FACTORIES:
        return _CORE_FACTORIES[member_id]()
    spec = specs.get(member_id)
    return _build_custom_agent(spec) if spec is not None else None


def _project_instructions(spec: store.ProjectSpec, member_ids: list[str], folder: str) -> list[str]:
    lines = [spec.instructions] if spec.instructions else []
    lines.append(f"You coordinate the {spec.name} project. Its workspace folder is '{folder}'.")
    if PROJECT_DEV_ID in member_ids:
        lines.append("Code, tests, reviews, security and performance work goes to Project Dev.")
    if PROJECT_RESEARCHER_ID in member_ids:
        lines.append(f"Research goes to Project Researcher, which saves its reports in '{folder}'.")
    lines.append("Answer directly only when no member is a better fit.")
    return lines


def _build_project_team(spec: store.ProjectSpec, specs: dict[str, store.AgentSpec]) -> Any | None:
    from agno.team import Team
    from agno.team.mode import TeamMode

    folder = spec.knowledge_folder or f"projects/{spec.project_id}"
    members: list[Any] = []
    member_ids: list[str] = []
    for member_id in dict.fromkeys(spec.members):
        if not _is_known_member(member_id, specs, allow_platform=False):
            logger.warning("Project %s: unknown member %r skipped", spec.project_id, member_id)
            continue
        member = _build_member(member_id, specs, allow_platform=False, project_folder=folder)
        if member is not None:
            members.append(member)
            member_ids.append(member_id)
    if not members:
        logger.warning("Project %s skipped; no members in %s", spec.project_id, spec.members)
        return None

    knowledge = _folder_knowledge(folder)
    return Team(
        id=spec.project_id,
        name=spec.name,
        description=spec.description or None,
        mode=TeamMode.coordinate,
        members=members,
        model=build_model(spec.model_id or settings.oap_manager_model),
        db=get_db(),
        knowledge=knowledge,
        search_knowledge=knowledge is not None,
        instructions=_project_instructions(spec, member_ids, folder),
        add_history_to_context=True,
        num_history_runs=10,
        markdown=True,
        store_events=settings.store_run_events,
    )


def _build_project_teams(specs: dict[str, store.AgentSpec]) -> dict[str, Any]:
    teams: dict[str, Any] = {}
    for spec in store.load_projects():
        if spec.project_id in specs or spec.project_id in store.RESERVED_AGENT_IDS:
            logger.warning("Project %s skipped; its id is taken by an agent", spec.project_id)
            continue
        try:
            team = _build_project_team(spec, specs)
        except Exception as exc:
            logger.error("Could not build project %s: %s", spec.project_id, exc)
            continue
        if team is not None:
            teams[spec.project_id] = team
    return teams


def _build_catalog_teams(specs: dict[str, store.AgentSpec]) -> dict[str, Any]:
    from agno.team import Team

    teams: dict[str, Any] = {}
    for spec in catalog_loader.team_files():
        if spec.team_id in _project_teams:
            logger.warning("Catalog team %s skipped; a project uses that id", spec.team_id)
            continue
        missing = [
            member
            for member in spec.members
            if not _is_known_member(member, specs, allow_platform=True)
        ]
        if missing or not spec.members:
            logger.warning("Catalog team %s skipped; missing members %s", spec.team_id, missing)
            continue
        members = [
            _build_member(member, specs, allow_platform=True)
            for member in dict.fromkeys(spec.members)
        ]
        if any(member is None for member in members):
            logger.warning("Catalog team %s skipped; a member failed to build", spec.team_id)
            continue
        teams[spec.team_id] = Team(
            id=spec.team_id,
            name=spec.name,
            members=members,
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


def _rebuild_registry_components() -> None:
    """Rebuild everything derived from registry.json and the catalog."""
    global _project_teams, _catalog_teams, _catalog_workflows
    specs = {spec.agent_id: spec for spec in store.load_specs()}
    _dynamic_agents.clear()
    _dynamic_agents.update(_build_dynamic_agents(specs))
    _project_teams = _build_project_teams(specs)
    _catalog_teams = _build_catalog_teams(specs)
    _catalog_workflows = _build_catalog_workflows({**_core_agents, **_dynamic_agents})


def initialize() -> None:
    """Build the platform group, the project group and all registered custom agents."""
    global _team
    catalog_loader.refresh(notify=False)
    scheduler_tools.set_db_provider(lambda: get_db())
    with _lock:
        _core_agents.clear()
        _core_agents.update(_build_core_agents())
        _project_templates.clear()
        _project_templates.update(_build_project_templates())
        _rebuild_registry_components()
        _team = _build_team(list(_build_core_agents().values()))


def reload_dynamic_agents() -> dict[str, Any]:
    """Rebuild custom agents and projects from the registry and notify the platform."""
    catalog_loader.refresh(notify=False)
    if not settings.llm_configured:
        return {}
    with _lock:
        if not _core_agents:
            initialize()
        else:
            _rebuild_registry_components()
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


def _ensure_initialized() -> None:
    if not _core_agents and settings.llm_configured:
        initialize()


def get_catalog_teams() -> list[Any]:
    _ensure_initialized()
    return list(_catalog_teams.values())


def get_catalog_workflows() -> list[Any]:
    _ensure_initialized()
    return list(_catalog_workflows.values())


def get_project_templates() -> dict[str, Any]:
    """The standalone `project-dev` team and `project-researcher` agent, by id."""
    _ensure_initialized()
    return dict(_project_templates)


def get_project_teams() -> list[Any]:
    """One team per user project."""
    _ensure_initialized()
    return list(_project_teams.values())


def get_team() -> Any:
    if _team is None:
        initialize()
    return _team


def get_all_agents_dict() -> dict[str, Any]:
    """Platform and custom agents by id (the project group is separate)."""
    if not _core_agents:
        initialize()
    return {**_core_agents, **_dynamic_agents}


def get_all_agents_list() -> list[Any]:
    return list(get_all_agents_dict().values())


def get_agentos_agents() -> list[Any]:
    """Top-level agents for AgentOS: platform, custom and `project-researcher`."""
    agents = get_all_agents_list()
    researcher = _project_templates.get(PROJECT_RESEARCHER_ID)
    return [*agents, researcher] if researcher is not None else agents


def get_agentos_teams() -> list[Any]:
    """Top-level teams for AgentOS: `oap`, `project-dev`, user projects and catalog teams."""
    team = get_team()
    project_dev = _project_templates.get(PROJECT_DEV_ID)
    return [
        team,
        *([project_dev] if project_dev is not None else []),
        *_project_teams.values(),
        *_catalog_teams.values(),
    ]


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
        settings.oap_skills_model,
        settings.oap_scheduler_model,
        settings.oap_dev_model,
        settings.oap_researcher_model,
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
    for components in (agents, _project_templates, _project_teams, _catalog_teams):
        if model_id in components:
            return components[model_id]
    if model_id in _catalog_workflows:
        return _catalog_workflows[model_id]
    studio = load_published_studio_agent(model_id)
    if studio is not None:
        return studio
    return get_team()


def _catalog_entry(component_id: str, component: Any, kind: str, group: str) -> dict[str, str]:
    return {
        "id": component_id,
        "name": getattr(component, "name", component_id) or component_id,
        "kind": kind,
        "group": group,
        "description": str(
            getattr(component, "description", None) or getattr(component, "role", None) or ""
        ),
    }


def describe_agents() -> list[dict[str, str]]:
    """Catalogue used by the `/v1/models` bridge endpoint.

    Every code-defined entry carries a `group`: `platform` for `oap` and the
    `platform-*` agents, `project` for the project group, `custom` for the rest.
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
            "group": "platform",
            "description": "Routes platform work to the right specialist agent",
        },
    ]
    entries.extend(
        _catalog_entry(
            agent_id,
            agent,
            "agent",
            "platform" if agent_id in store.PLATFORM_AGENT_IDS else "custom",
        )
        for agent_id, agent in agents.items()
    )
    entries.extend(
        _catalog_entry(template_id, template, _PROJECT_TEMPLATE_KINDS[template_id], "project")
        for template_id, template in _project_templates.items()
    )
    entries.extend(
        _catalog_entry(team_id, team, "team", "project") for team_id, team in _project_teams.items()
    )
    entries.extend(
        _catalog_entry(team_id, team, "team", "custom") for team_id, team in _catalog_teams.items()
    )
    entries.extend(
        _catalog_entry(workflow_id, workflow, "workflow", "custom")
        for workflow_id, workflow in _catalog_workflows.items()
    )
    entries.extend(list_published_studio_agents({entry["id"] for entry in entries}))
    return entries


# Any registry mutation (from a tool call or a direct edit) rebuilds the
# dynamic agents and projects without `store` ever importing this module.
store.subscribe(lambda: reload_dynamic_agents())
