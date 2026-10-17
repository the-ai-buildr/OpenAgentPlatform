"""The Project group: project-dev, project-researcher and user-created projects.

Everything is built for real with Agno, but offline: the model is an OpenRouter
client holding a fake key (no request is made at construction) and the
database is Agno's in-memory store instead of Postgres.
"""

import json
import sys

import pytest
from agno.db.in_memory import InMemoryDb
from agno.os import AgentOS
from agno.team.mode import TeamMode

from app import catalog_loader
from app.agents import runtime, store
from app.tools import platform_tools

DEV_SUBAGENT_SKILLS = {
    "dev-code-reviewer": {
        "code-review-and-quality",
        "code-simplification",
        "api-and-interface-design",
    },
    "dev-test-engineer": {
        "test-driven-development",
        "debugging-and-error-recovery",
        "browser-testing-with-devtools",
    },
    "dev-security-auditor": {"security-and-hardening", "deprecation-and-migration"},
    "dev-perf-auditor": {
        "performance-optimization",
        "frontend-ui-engineering",
        "observability-and-instrumentation",
    },
}


@pytest.fixture()
def offline(monkeypatch, tmp_path, registry_file, workspace):
    monkeypatch.setattr(runtime.settings, "openrouter_api_key", "sk-test")
    monkeypatch.setattr(runtime.settings, "store_run_events", True)
    monkeypatch.setattr(runtime.km, "embeddings_available", lambda: False)
    db = InMemoryDb()
    monkeypatch.setattr(runtime, "get_db", lambda: db)
    monkeypatch.setattr(runtime, "list_published_studio_agents", lambda exclude: [])
    monkeypatch.setattr(runtime, "load_published_studio_agent", lambda agent_id: None)
    monkeypatch.setattr(platform_tools, "sync_openwebui_models", lambda: {"status": "skipped"})
    for name, value in {
        "_core_agents": {},
        "_dynamic_agents": {},
        "_project_templates": {},
        "_project_teams": {},
        "_catalog_teams": {},
        "_catalog_workflows": {},
        "_team": None,
        "_on_change": [],
    }.items():
        monkeypatch.setattr(runtime, name, value)

    catalog = tmp_path / "catalog"
    for name, sub in {
        "AGENTS_DIR": "agents",
        "TEAMS_DIR": "teams",
        "WORKFLOWS_DIR": "workflows",
        "PROJECTS_DIR": "projects",
    }.items():
        monkeypatch.setattr(catalog_loader, name, catalog / sub)
    monkeypatch.setattr(catalog_loader, "WORKSPACE_ROOT", workspace)
    monkeypatch.setattr(catalog_loader, "_team_files", [])
    monkeypatch.setattr(catalog_loader, "_workflow_files", [])
    return catalog


def _tool_names(component):
    return {getattr(t, "name", None) or getattr(t, "__name__", "") for t in component.tools or []}


def _toolkit_classes(component):
    return {type(t).__name__ for t in component.tools or []}


def _skill_names(component):
    return {skill.name for skill in component.skills.get_all_skills()}


def _create_acme(**overrides):
    helper = store.AgentSpec(
        agent_id="helper", name="Helper", skills=["git-workflow-and-versioning"]
    )
    store.upsert_spec(helper)
    payload = {
        "project_id": "acme",
        "name": "Acme Launch",
        "description": "Launch site for Acme",
        "members": ["project-dev", "project-researcher", "helper", "ghost"],
        **overrides,
    }
    result = platform_tools.create_or_update_project(**payload)
    assert result["status"] == "ok", result


def test_project_dev_team_has_four_specialists_with_skills(offline):
    team = runtime.build_project_dev_team()

    assert team.id == "project-dev"
    assert team.mode == TeamMode.coordinate
    assert team.model.id == runtime.settings.oap_dev_model
    assert team.store_events is True
    assert [member.id for member in team.members] == list(DEV_SUBAGENT_SKILLS)
    skills, tool_names = _skill_names(team), _tool_names(team)
    assert {"human-in-the-loop", "using-agent-skills", "code-review-and-quality"} <= skills
    assert {"list_project_files", "read_source_file", "save_text_file_to_folder"} <= tool_names
    for member in team.members:
        assert _skill_names(member) == DEV_SUBAGENT_SKILLS[member.id]
        assert _tool_names(member) == {"list_project_files", "read_source_file"}


def test_project_researcher_assembles_research_toolkits(offline):
    agent = runtime.build_project_researcher()

    assert agent.id == "project-researcher"
    assert agent.model.id == runtime.settings.oap_researcher_model
    toolkits = _toolkit_classes(agent)
    assert {"DuckDuckGoTools", "ArxivTools", "Newspaper4kTools", "WebsiteTools"} <= toolkits
    assert {
        "save_text_file_to_folder",
        "list_workspace_directories_and_files",
        "inspect_research_toolkits",
    } <= _tool_names(agent)
    assert _skill_names(agent) == set(runtime.RESEARCHER_SKILLS)
    assert any("'research'" in line for line in agent.instructions)


def test_project_researcher_survives_a_missing_optional_toolkit(offline, monkeypatch):
    monkeypatch.setitem(sys.modules, "agno.tools.arxiv", None)

    agent = runtime.build_project_researcher()

    classes = _toolkit_classes(agent)
    assert "ArxivTools" not in classes
    assert {"DuckDuckGoTools", "WebsiteTools"} <= classes
    assert "save_text_file_to_folder" in _tool_names(agent)


def test_store_events_follows_the_setting(offline, monkeypatch):
    monkeypatch.setattr(runtime.settings, "store_run_events", False)

    team = runtime.build_project_dev_team()

    assert team.store_events is False
    assert all(member.store_events is False for member in team.members)
    assert runtime.build_project_researcher().store_events is False


def test_platform_group_gains_skills_and_scheduler_agents(offline):
    runtime.initialize()
    agents = runtime.get_all_agents_dict()

    skills_agent = agents["platform-skills"]
    assert skills_agent.model.id == runtime.settings.oap_skills_model
    skill_tools = _tool_names(skills_agent)
    assert {"list_platform_skills", "delete_custom_skill", "create_or_update_agent"} <= skill_tools
    assert "human-in-the-loop" in _skill_names(skills_agent)

    scheduler = agents["platform-scheduler"]
    assert scheduler.model.id == runtime.settings.oap_scheduler_model
    scheduler_tools = _tool_names(scheduler)
    assert {"create_schedule", "delete_schedule", "check_background_run_status"} <= scheduler_tools
    assert "check_background_run_status" in _tool_names(agents["platform-manager"])
    assert all(agent.store_events is True for agent in agents.values())
    assert runtime.get_team().store_events is True


def test_project_group_is_not_part_of_the_oap_team(offline):
    runtime.initialize()

    member_ids = [member.id for member in runtime.get_team().members]
    assert member_ids == list(runtime.CORE_AGENT_IDS)
    assert "project-dev" not in member_ids
    assert "project-researcher" not in member_ids


def test_oap_team_members_are_separate_from_standalone_platform_agents(offline):
    runtime.initialize()
    team = runtime.get_team()
    standalone = runtime.get_all_agents_dict()

    team.initialize_team()

    for member in team.members:
        assert member is not standalone[member.id]
        assert member.team_id == runtime.TEAM_ID
    for agent_id in runtime.CORE_AGENT_IDS:
        assert standalone[agent_id].team_id is None


def test_user_project_attaches_fresh_project_dev_and_researcher(offline):
    _create_acme(model_id="openai/gpt-5.5")
    platform_tools.create_or_update_project(project_id="empty", name="Empty", members=["ghost"])

    runtime.initialize()

    projects = {team.id: team for team in runtime.get_project_teams()}
    assert set(projects) == {"acme"}
    acme = projects["acme"]
    assert acme.name == "Acme Launch"
    assert acme.model.id == "openai/gpt-5.5"
    assert acme.store_events is True
    assert [member.id for member in acme.members] == ["project-dev", "project-researcher", "helper"]

    templates = runtime.get_project_templates()
    dev, researcher, helper = acme.members
    assert dev is not templates["project-dev"]
    assert researcher is not templates["project-researcher"]
    assert helper is not runtime.get_all_agents_dict()["helper"]
    assert [member.id for member in dev.members] == list(DEV_SUBAGENT_SKILLS)
    assert any("projects/acme" in line for line in researcher.instructions)
    assert _skill_names(helper) == {"git-workflow-and-versioning"}


def test_project_without_a_model_uses_the_manager_model(offline):
    _create_acme()

    runtime.initialize()

    (acme,) = runtime.get_project_teams()
    assert acme.model.id == runtime.settings.oap_manager_model


def test_reload_rebuilds_projects_but_keeps_templates(offline):
    runtime.initialize()
    templates = runtime.get_project_templates()
    assert runtime.get_project_teams() == []

    _create_acme()
    runtime.reload_dynamic_agents()

    assert [team.id for team in runtime.get_project_teams()] == ["acme"]
    rebuilt = runtime.get_project_templates()
    assert all(rebuilt[template_id] is templates[template_id] for template_id in templates)


def test_describe_agents_tags_platform_project_and_custom_groups(offline):
    _create_acme()
    runtime.initialize()

    catalog = {entry["id"]: (entry["kind"], entry["group"]) for entry in runtime.describe_agents()}

    assert catalog["oap"] == ("team", "platform")
    for agent_id in runtime.CORE_AGENT_IDS:
        assert catalog[agent_id] == ("agent", "platform")
    assert catalog["project-dev"] == ("team", "project")
    assert catalog["project-researcher"] == ("agent", "project")
    assert catalog["acme"] == ("team", "project")
    assert catalog["helper"] == ("agent", "custom")


def test_get_runner_resolves_the_project_group(offline):
    _create_acme()
    runtime.initialize()
    templates = runtime.get_project_templates()
    agents = runtime.get_all_agents_dict()

    assert runtime.get_runner("project-dev") is templates["project-dev"]
    assert runtime.get_runner("project-researcher") is templates["project-researcher"]
    assert runtime.get_runner("acme") is runtime.get_project_teams()[0]
    assert runtime.get_runner("platform-scheduler") is agents["platform-scheduler"]
    assert runtime.get_runner("no-such-model") is runtime.get_team()


def test_catalog_teams_and_projects_can_use_the_project_group(offline):
    teams_dir = offline / "teams"
    projects_dir = offline / "projects"
    teams_dir.mkdir(parents=True)
    projects_dir.mkdir(parents=True)
    (teams_dir / "desk.json").write_text(
        json.dumps({"name": "Desk", "members": ["project-researcher", "platform-manager"]})
    )
    (projects_dir / "zeta.json").write_text(json.dumps({"name": "Zeta"}))

    runtime.initialize()

    (desk,) = runtime.get_catalog_teams()
    assert [member.id for member in desk.members] == ["project-researcher", "platform-manager"]
    assert desk.members[0] is not runtime.get_project_templates()["project-researcher"]
    assert desk.members[1] is not runtime.get_all_agents_dict()["platform-manager"]

    (zeta,) = runtime.get_project_teams()
    assert [member.id for member in zeta.members] == ["project-dev", "project-researcher"]
    spec = store.get_project("zeta")
    assert spec.knowledge_folder == "projects/zeta"
    assert spec.extra["source"] == "catalog"


def test_agentos_accepts_the_project_group(offline):
    _create_acme()
    runtime.initialize()

    agents = runtime.get_agentos_agents()
    teams = runtime.get_agentos_teams()
    assert "project-researcher" in [agent.id for agent in agents]
    assert [team.id for team in teams] == ["oap", "project-dev", "acme"]

    # Member ids repeat across teams (project-dev inside acme); only top-level
    # ids must be unique for AgentOS.
    db = runtime.get_db()
    agent_os = AgentOS(id="oap-test", agents=agents, teams=teams, db=db, telemetry=False)
    assert [team.id for team in agent_os.teams] == ["oap", "project-dev", "acme"]
