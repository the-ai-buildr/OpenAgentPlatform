"""Catalog files become registry agents with a default knowledge folder."""

import json

from app.agents.store import AgentSpec, load_specs, sync_catalog_agents, upsert_spec
from app.catalog_loader import (
    component_id,
    read_agent_files,
    read_project_files,
    read_team_files,
    read_workflow_files,
)


def test_example_files_are_not_registered(tmp_path):
    example = tmp_path / "_example.json"
    example.write_text(json.dumps({"name": "Hidden"}))
    real = tmp_path / "research.json"
    real.write_text(
        json.dumps(
            {
                "name": "Research Analyst",
                "instructions": "Cite sources.",
                "enable_web_search": True,
            }
        )
    )

    assert component_id(example) is None
    specs = read_agent_files(tmp_path)
    assert [spec.agent_id for spec in specs] == ["research"]
    assert specs[0].name == "Research Analyst"
    assert specs[0].knowledge_folder == "agents/research"
    assert specs[0].enable_web_search is True


def test_catalog_sync_replaces_catalog_agents_and_keeps_builder_agents(registry_file):
    upsert_spec(AgentSpec(agent_id="handmade", name="Handmade", extra={"source": "builder"}))
    upsert_spec(AgentSpec(agent_id="old-file", name="Old", extra={"source": "catalog"}))

    changed = sync_catalog_agents(
        [AgentSpec(agent_id="research", name="Research Analyst", instructions="Cite sources.")],
        notify=False,
    )

    assert changed is True
    by_id = {spec.agent_id: spec for spec in load_specs()}
    assert set(by_id) == {"handmade", "research"}
    assert by_id["research"].knowledge_folder == "agents/research"
    assert by_id["research"].extra["source"] == "catalog"
    assert by_id["handmade"].name == "Handmade"


def test_team_and_workflow_files_keep_their_members(tmp_path):
    teams_dir = tmp_path / "teams"
    workflows_dir = tmp_path / "workflows"
    teams_dir.mkdir()
    workflows_dir.mkdir()
    (teams_dir / "desk.json").write_text(
        json.dumps({"name": "Research Desk", "members": ["research"], "instructions": "Delegate."})
    )
    (workflows_dir / "brief.json").write_text(
        json.dumps({"name": "Brief", "agent_id": "research", "description": "A short brief."})
    )

    teams = read_team_files(teams_dir)
    workflows = read_workflow_files(workflows_dir)
    assert teams[0].team_id == "desk"
    assert teams[0].members == ["research"]
    assert workflows[0].workflow_id == "brief"
    assert workflows[0].agent_id == "research"


def test_project_files_default_to_a_project_folder_and_the_project_group(tmp_path):
    (tmp_path / "_example.json").write_text(json.dumps({"name": "Hidden"}))
    (tmp_path / "project-dev.json").write_text(json.dumps({"name": "Shadow"}))
    (tmp_path / "acme.json").write_text(json.dumps({"name": "Acme", "description": "Launch"}))
    (tmp_path / "solo.json").write_text(json.dumps({"name": "Solo", "members": ["research"]}))

    projects = {project.project_id: project for project in read_project_files(tmp_path)}

    assert set(projects) == {"acme", "solo"}
    assert projects["acme"].knowledge_folder == "projects/acme"
    assert projects["acme"].members == ["project-dev", "project-researcher"]
    assert projects["acme"].description == "Launch"
    assert projects["solo"].members == ["research"]
