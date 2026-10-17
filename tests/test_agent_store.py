"""Registry upsert, delete and folder-resolution rules for agents and projects."""

import json

import pytest

from app.agents.store import (
    DEFAULT_PROJECT_MEMBERS,
    PLATFORM_AGENT_IDS,
    PROJECT_TEMPLATE_IDS,
    RESERVED_AGENT_IDS,
    AgentSpec,
    ProjectSpec,
    RegistryError,
    bind_folder_to_agent_if_custom,
    delete_project_spec,
    delete_spec,
    get_project,
    get_spec,
    load_projects,
    load_specs,
    resolve_agent_target_folder,
    save_projects,
    save_specs,
    subscribe,
    sync_catalog_agents,
    sync_catalog_projects,
    upsert_project,
    upsert_spec,
)


def spec(agent_id="research", **kwargs):
    return AgentSpec(agent_id=agent_id, name=kwargs.pop("name", "Research"), **kwargs)


def project(project_id="acme", **kwargs):
    return ProjectSpec(project_id=project_id, name=kwargs.pop("name", "Acme"), **kwargs)


class TestReservedGroups:
    def test_platform_group(self):
        assert {
            "platform-builder",
            "platform-engineer",
            "platform-manager",
            "platform-telemetry",
            "platform-skills",
            "platform-scheduler",
        } == PLATFORM_AGENT_IDS

    def test_project_group(self):
        assert {
            "project-dev",
            "project-researcher",
            "dev-code-reviewer",
            "dev-test-engineer",
            "dev-security-auditor",
            "dev-perf-auditor",
        } == PROJECT_TEMPLATE_IDS

    def test_reserved_ids_cover_both_groups_and_the_coordinator(self):
        assert PLATFORM_AGENT_IDS | PROJECT_TEMPLATE_IDS | {"oap"} == RESERVED_AGENT_IDS
        assert not PLATFORM_AGENT_IDS & PROJECT_TEMPLATE_IDS

    def test_default_project_members(self):
        assert DEFAULT_PROJECT_MEMBERS == ("project-dev", "project-researcher")
        assert project().members == ["project-dev", "project-researcher"]


class TestUpsert:
    def test_creates_a_new_agent(self, registry_file):
        upsert_spec(spec())
        assert [s.agent_id for s in load_specs()] == ["research"]

    def test_updates_in_place_without_duplicating(self, registry_file):
        upsert_spec(spec())
        upsert_spec(spec(name="Research v2"))
        specs = load_specs()
        assert len(specs) == 1
        assert specs[0].name == "Research v2"

    def test_update_keeps_an_existing_folder_binding(self, registry_file):
        upsert_spec(spec(knowledge_folder="contracts"))
        upsert_spec(spec(name="Research v2"))
        assert get_spec("research").knowledge_folder == "contracts"

    def test_update_can_move_the_folder_explicitly(self, registry_file):
        upsert_spec(spec(knowledge_folder="contracts"))
        upsert_spec(spec(knowledge_folder="sops"))
        assert get_spec("research").knowledge_folder == "sops"

    def test_normalises_the_folder(self, registry_file):
        upsert_spec(spec(knowledge_folder="/Contracts/Acme Corp/"))
        assert get_spec("research").knowledge_folder == "contracts/acme-corp"

    @pytest.mark.parametrize("reserved", sorted(RESERVED_AGENT_IDS))
    def test_rejects_reserved_ids(self, registry_file, reserved):
        with pytest.raises(RegistryError):
            upsert_spec(spec(agent_id=reserved))
        assert load_specs() == []

    def test_rejects_a_blank_id(self, registry_file):
        with pytest.raises(RegistryError):
            upsert_spec(spec(agent_id="  "))

    def test_preserves_unknown_fields_across_updates(self, registry_file):
        save_specs([AgentSpec.from_dict({"agent_id": "research", "name": "R", "owner": "tyler"})])
        upsert_spec(spec(name="R2"))
        raw = json.loads(registry_file.read_text())["custom_agents"][0]
        assert raw["owner"] == "tyler"

    def test_writes_valid_json_atomically(self, registry_file):
        upsert_spec(spec())
        payload = json.loads(registry_file.read_text())
        assert payload["custom_agents"][0]["agent_id"] == "research"
        assert not list(registry_file.parent.glob("*.tmp"))


class TestAgentSkills:
    def test_defaults_to_no_skills(self, registry_file):
        upsert_spec(spec())
        assert get_spec("research").skills == []
        assert json.loads(registry_file.read_text())["custom_agents"][0]["skills"] == []

    def test_persists_skill_names(self, registry_file):
        upsert_spec(spec(skills=["test-driven-development", " human-in-the-loop ", ""]))
        assert get_spec("research").skills == ["test-driven-development", "human-in-the-loop"]

    def test_update_without_skills_keeps_existing_bindings(self, registry_file):
        upsert_spec(spec(skills=["*"]))
        upsert_spec(spec(name="Research v2"))
        assert get_spec("research").skills == ["*"]

    def test_update_can_replace_skills(self, registry_file):
        upsert_spec(spec(skills=["code-review-and-quality"]))
        upsert_spec(spec(skills=["security-and-hardening"]))
        assert get_spec("research").skills == ["security-and-hardening"]

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (["a", "b"], ["a", "b"]),
            ("a, b ,", ["a", "b"]),
            (None, []),
            (42, []),
        ],
    )
    def test_from_dict_parses_lists_and_comma_strings(self, raw, expected):
        parsed = AgentSpec.from_dict({"agent_id": "x", "skills": raw})
        assert parsed.skills == expected
        assert "skills" not in parsed.extra


class TestLoad:
    def test_missing_file_is_empty(self, registry_file):
        assert load_specs() == []

    def test_corrupt_file_is_empty_rather_than_fatal(self, registry_file):
        registry_file.write_text("{not json")
        assert load_specs() == []

    def test_entries_without_an_id_are_ignored(self, registry_file):
        registry_file.write_text(json.dumps({"custom_agents": [{"name": "orphan"}]}))
        assert load_specs() == []


class TestDelete:
    def test_removes_an_agent(self, registry_file):
        upsert_spec(spec())
        assert delete_spec("research") is True
        assert load_specs() == []

    def test_unknown_agent_reports_false(self, registry_file):
        assert delete_spec("nope") is False


class TestResolveTargetFolder:
    def test_explicit_folder_wins(self, registry_file):
        upsert_spec(spec(knowledge_folder="contracts"))
        assert resolve_agent_target_folder("research", "sops") == "sops"

    def test_bound_folder_is_used_next(self, registry_file):
        upsert_spec(spec(knowledge_folder="contracts"))
        assert resolve_agent_target_folder("research") == "contracts"

    @pytest.mark.parametrize("agent_id", sorted(RESERVED_AGENT_IDS - {"project-researcher"}))
    def test_core_agents_default_to_general(self, registry_file, agent_id):
        assert resolve_agent_target_folder(agent_id) == "general"

    def test_project_researcher_writes_to_research(self, registry_file):
        assert resolve_agent_target_folder("project-researcher") == "research"
        assert resolve_agent_target_folder("project-researcher", "projects/acme") == "projects/acme"

    def test_user_projects_use_their_bound_folder(self, registry_file):
        upsert_project(project())
        upsert_project(project("globex", knowledge_folder="Clients/Globex"))
        assert resolve_agent_target_folder("acme") == "projects/acme"
        assert resolve_agent_target_folder("globex") == "clients/globex"
        assert resolve_agent_target_folder("acme", "sops") == "sops"

    def test_unknown_agent_gets_its_own_folder(self, registry_file):
        assert resolve_agent_target_folder("research") == "agents/research"

    def test_no_agent_defaults_to_general(self, registry_file):
        assert resolve_agent_target_folder(None) == "general"

    def test_explicit_folder_is_normalised(self, registry_file):
        assert resolve_agent_target_folder("platform-builder", "/SOPs/") == "sops"


class TestBindFolder:
    def test_binds_an_unbound_custom_agent(self, registry_file):
        upsert_spec(spec())
        assert bind_folder_to_agent_if_custom("research", "contracts") is True
        assert get_spec("research").knowledge_folder == "contracts"

    def test_does_not_repoint_an_already_bound_agent(self, registry_file):
        upsert_spec(spec(knowledge_folder="contracts"))
        assert bind_folder_to_agent_if_custom("research", "sops") is False
        assert get_spec("research").knowledge_folder == "contracts"

    @pytest.mark.parametrize("agent_id", ["platform-builder", "platform-skills", "project-dev"])
    def test_ignores_core_agents(self, registry_file, agent_id):
        assert bind_folder_to_agent_if_custom(agent_id, "sops") is False

    def test_ignores_projects(self, registry_file):
        upsert_project(project())
        assert bind_folder_to_agent_if_custom("acme", "sops") is False
        assert get_project("acme").knowledge_folder == "projects/acme"

    def test_ignores_unknown_agents(self, registry_file):
        assert bind_folder_to_agent_if_custom("ghost", "sops") is False


class TestSubscribers:
    def test_mutations_notify_listeners(self, registry_file):
        calls = []
        subscribe(lambda: calls.append(1))
        upsert_spec(spec())
        delete_spec("research")
        assert len(calls) == 2

    def test_project_mutations_notify_listeners(self, registry_file):
        calls = []
        subscribe(lambda: calls.append(1))
        upsert_project(project())
        delete_project_spec("acme")
        delete_project_spec("acme")
        assert len(calls) == 2


# --------------------------------------------------------------------------- #
# Projects
# --------------------------------------------------------------------------- #
class TestProjectSpec:
    def test_from_dict_accepts_team_and_id_aliases(self):
        assert ProjectSpec.from_dict({"team_id": "acme"}).project_id == "acme"
        assert ProjectSpec.from_dict({"id": "acme"}).project_id == "acme"
        parsed = ProjectSpec.from_dict({"team_id": "acme", "id": "x", "owner": "tyler"})
        assert parsed.extra == {"owner": "tyler"}

    def test_from_dict_defaults(self):
        parsed = ProjectSpec.from_dict({"project_id": "acme"})
        assert parsed.name == "acme"
        assert parsed.members == ["project-dev", "project-researcher"]
        assert parsed.knowledge_folder is None

    def test_from_dict_reads_role_as_description_and_comma_members(self):
        parsed = ProjectSpec.from_dict(
            {"project_id": "acme", "role": "Client work", "members": "project-dev, analyst"}
        )
        assert parsed.description == "Client work"
        assert parsed.members == ["project-dev", "analyst"]

    def test_to_dict_round_trips_extras(self):
        parsed = ProjectSpec.from_dict({"project_id": "acme", "owner": "tyler"})
        data = parsed.to_dict()
        assert data["owner"] == "tyler"
        assert data["project_id"] == "acme"
        assert ProjectSpec.from_dict(data) == parsed


class TestUpsertProject:
    def test_creates_with_default_folder_and_members(self, registry_file):
        created = upsert_project(project())
        assert created.knowledge_folder == "projects/acme"
        assert created.members == ["project-dev", "project-researcher"]
        assert [p.project_id for p in load_projects()] == ["acme"]

    def test_normalises_an_explicit_folder_and_strips_the_id(self, registry_file):
        upsert_project(project("  acme  ", knowledge_folder="/Clients/Acme Corp/"))
        assert get_project("acme").knowledge_folder == "clients/acme-corp"

    def test_update_keeps_the_existing_folder(self, registry_file):
        upsert_project(project(knowledge_folder="clients/acme"))
        upsert_project(project(name="Acme v2"))
        saved = get_project("acme")
        assert saved.name == "Acme v2"
        assert saved.knowledge_folder == "clients/acme"

    def test_update_can_move_the_folder(self, registry_file):
        upsert_project(project())
        upsert_project(project(knowledge_folder="clients/acme"))
        assert get_project("acme").knowledge_folder == "clients/acme"

    def test_custom_members_are_kept_and_empty_members_fall_back(self, registry_file):
        upsert_project(project(members=["project-dev", "analyst", " "]))
        assert get_project("acme").members == ["project-dev", "analyst"]
        upsert_project(project(members=[]))
        assert get_project("acme").members == ["project-dev", "project-researcher"]

    def test_merges_extras_across_updates(self, registry_file):
        save_projects([ProjectSpec.from_dict({"project_id": "acme", "owner": "tyler"})])
        upsert_project(project(name="Acme v2"))
        raw = json.loads(registry_file.read_text())["projects"][0]
        assert raw["owner"] == "tyler"

    @pytest.mark.parametrize("reserved", sorted(RESERVED_AGENT_IDS))
    def test_rejects_reserved_ids(self, registry_file, reserved):
        with pytest.raises(RegistryError):
            upsert_project(project(reserved))
        assert load_projects() == []

    def test_rejects_a_blank_id(self, registry_file):
        with pytest.raises(RegistryError):
            upsert_project(project("   "))


class TestRegistryCoexistence:
    def test_agents_and_projects_share_one_file(self, registry_file):
        upsert_spec(spec())
        upsert_project(project())
        payload = json.loads(registry_file.read_text())
        assert [a["agent_id"] for a in payload["custom_agents"]] == ["research"]
        assert [p["project_id"] for p in payload["projects"]] == ["acme"]

    def test_agent_writes_preserve_projects(self, registry_file):
        upsert_project(project())
        upsert_spec(spec())
        delete_spec("research")
        assert [p.project_id for p in load_projects()] == ["acme"]

    def test_project_writes_preserve_agents(self, registry_file):
        upsert_spec(spec())
        upsert_project(project())
        delete_project_spec("acme")
        assert [s.agent_id for s in load_specs()] == ["research"]
        assert load_projects() == []

    def test_legacy_file_without_projects_loads(self, registry_file):
        registry_file.write_text(json.dumps({"custom_agents": [{"agent_id": "research"}]}))
        assert load_projects() == []
        assert [s.agent_id for s in load_specs()] == ["research"]

    def test_project_entries_without_an_id_are_ignored(self, registry_file):
        registry_file.write_text(json.dumps({"projects": [{"name": "orphan"}, {"team_id": "t"}]}))
        assert [p.project_id for p in load_projects()] == ["t"]


class TestIdCollisions:
    def test_project_cannot_take_a_custom_agent_id(self, registry_file):
        upsert_spec(spec("acme"))
        with pytest.raises(RegistryError, match="custom agent"):
            upsert_project(project("acme"))
        assert load_projects() == []
        assert [s.agent_id for s in load_specs()] == ["acme"]

    def test_custom_agent_cannot_take_a_project_id(self, registry_file):
        upsert_project(project("acme"))
        with pytest.raises(RegistryError, match="project"):
            upsert_spec(spec("acme"))
        assert load_specs() == []
        assert [p.project_id for p in load_projects()] == ["acme"]

    def test_catalog_agent_colliding_with_a_project_is_skipped(self, registry_file, caplog):
        upsert_project(project("acme"))
        assert sync_catalog_agents([spec("acme"), spec("research")], notify=False) is True
        assert [s.agent_id for s in load_specs()] == ["research"]
        assert [p.project_id for p in load_projects()] == ["acme"]
        assert "Skipping catalog agent acme" in caplog.text

    def test_catalog_project_colliding_with_an_agent_is_skipped(self, registry_file, caplog):
        upsert_spec(spec("research"))
        assert sync_catalog_projects([project("research"), project("acme")], notify=False) is True
        assert [p.project_id for p in load_projects()] == ["acme"]
        assert [s.agent_id for s in load_specs()] == ["research"]
        assert "Skipping catalog project research" in caplog.text


class TestDeleteProject:
    def test_removes_a_project(self, registry_file):
        upsert_project(project())
        assert delete_project_spec("acme") is True
        assert get_project("acme") is None

    def test_unknown_project_reports_false(self, registry_file):
        assert delete_project_spec("nope") is False


class TestSyncCatalogProjects:
    def test_adds_catalog_projects_with_defaults(self, registry_file):
        assert sync_catalog_projects([project()], notify=False) is True
        saved = get_project("acme")
        assert saved.extra["source"] == "catalog"
        assert saved.knowledge_folder == "projects/acme"
        assert saved.members == ["project-dev", "project-researcher"]

    def test_keeps_user_projects_and_drops_stale_catalog_ones(self, registry_file):
        upsert_project(project("mine"))
        sync_catalog_projects([project("acme"), project("globex")], notify=False)
        assert sync_catalog_projects([project("acme")], notify=False) is True
        assert sorted(p.project_id for p in load_projects()) == ["acme", "mine"]
        assert "source" not in get_project("mine").extra

    def test_unchanged_catalog_is_a_no_op(self, registry_file):
        sync_catalog_projects([project()], notify=False)
        assert sync_catalog_projects([project()], notify=False) is False

    def test_changed_catalog_entry_is_replaced(self, registry_file):
        sync_catalog_projects([project()], notify=False)
        assert sync_catalog_projects([project(name="Acme Renamed")], notify=False) is True
        assert get_project("acme").name == "Acme Renamed"

    def test_notify_flag_controls_listeners(self, registry_file):
        calls = []
        subscribe(lambda: calls.append(1))
        sync_catalog_projects([project("a")], notify=False)
        sync_catalog_projects([project("b")])
        assert len(calls) == 1

    @pytest.mark.parametrize("bad_id", ["project-dev", "oap", "  "])
    def test_rejects_reserved_or_blank_ids(self, registry_file, bad_id):
        with pytest.raises(RegistryError):
            sync_catalog_projects([project(bad_id)], notify=False)


# --------------------------------------------------------------------------- #
# Platform registry tools
# --------------------------------------------------------------------------- #
@pytest.fixture()
def tools(registry_file, workspace, monkeypatch):
    from app.tools import platform_tools

    syncs: list[int] = []

    def fake_sync():
        syncs.append(1)
        return {"status": "ok"}

    monkeypatch.setattr(platform_tools, "sync_openwebui_models", fake_sync)
    platform_tools.syncs = syncs
    yield platform_tools
    del platform_tools.syncs


class TestProjectTools:
    def test_create_project_with_default_members_and_folder(self, tools, workspace):
        result = tools.create_or_update_project(project_id="acme", name="Acme", description="Client")
        assert result["status"] == "ok"
        assert result["openwebui_sync"] == {"status": "ok"}
        created = result["project"]
        assert created["members"] == ["project-dev", "project-researcher"]
        assert created["knowledge_folder"] == "projects/acme"
        assert created["description"] == "Client"
        assert (workspace / "projects" / "acme").is_dir()
        assert get_project("acme").members == ["project-dev", "project-researcher"]

    def test_create_project_with_custom_members_and_folder(self, tools, workspace):
        upsert_spec(spec("analyst"))
        result = tools.create_or_update_project(
            project_id="globex",
            name="Globex",
            members=["project-researcher", "analyst"],
            knowledge_folder="Clients/Globex",
            model_id="openai/gpt-5",
        )
        assert result["status"] == "ok"
        assert result["project"]["members"] == ["project-researcher", "analyst"]
        assert result["project"]["knowledge_folder"] == "clients/globex"
        assert result["project"]["model_id"] == "openai/gpt-5"
        assert (workspace / "clients" / "globex").is_dir()

    def test_update_without_folder_keeps_the_bound_folder(self, tools, workspace):
        tools.create_or_update_project(project_id="acme", name="Acme", knowledge_folder="clients/acme")
        result = tools.create_or_update_project(project_id="acme", name="Acme v2")
        assert result["status"] == "ok"
        assert result["project"]["knowledge_folder"] == "clients/acme"
        assert get_project("acme").knowledge_folder == "clients/acme"

    def test_update_without_members_keeps_attached_members(self, tools):
        tools.create_or_update_project(project_id="acme", name="Acme", members=["project-dev", "analyst"])
        tools.create_or_update_project(project_id="acme", name="Acme", description="new")
        saved = get_project("acme")
        assert saved.members == ["project-dev", "analyst"]
        assert saved.description == "new"

    def test_update_can_replace_members(self, tools):
        tools.create_or_update_project(project_id="acme", name="Acme", members=["analyst"])
        tools.create_or_update_project(project_id="acme", name="Acme", members=["project-dev"])
        assert get_project("acme").members == ["project-dev"]

    def test_blank_name_falls_back_to_the_id(self, tools):
        assert tools.create_or_update_project(project_id="acme", name="  ")["project"]["name"] == "acme"

    @pytest.mark.parametrize("reserved", ["project-dev", "project-researcher", "platform-skills", "oap"])
    def test_reserved_ids_are_rejected_without_side_effects(self, tools, workspace, reserved):
        result = tools.create_or_update_project(project_id=reserved, name="x")
        assert result["status"] == "error"
        assert "RegistryError" in result["error"]
        assert load_projects() == []
        assert not (workspace / "projects" / reserved).exists()
        assert tools.syncs == []

    def test_blank_id_is_rejected(self, tools):
        assert tools.create_or_update_project(project_id="  ", name="x")["status"] == "error"

    def test_custom_agent_id_is_rejected_without_side_effects(self, tools, workspace):
        upsert_spec(spec("acme"))
        result = tools.create_or_update_project(project_id="acme", name="Acme")
        assert result["status"] == "error"
        assert "RegistryError" in result["error"]
        assert load_projects() == []
        assert not (workspace / "projects" / "acme").exists()
        assert tools.syncs == []

    def test_unsafe_folder_is_rejected(self, tools):
        result = tools.create_or_update_project(project_id="acme", name="A", knowledge_folder="../etc")
        assert result["status"] == "error"
        assert load_projects() == []

    def test_list_projects(self, tools):
        tools.create_or_update_project(project_id="acme", name="Acme")
        result = tools.list_projects()
        assert result["status"] == "ok"
        assert result["project_templates"] == ["project-dev", "project-researcher"]
        assert [p["project_id"] for p in result["projects"]] == ["acme"]

    def test_delete_project_is_directly_callable(self, tools):
        tools.create_or_update_project(project_id="acme", name="Acme")
        tools.syncs.clear()
        result = tools.delete_project("acme")
        assert result == {"status": "ok", "project_id": "acme", "openwebui_sync": {"status": "ok"}}
        assert load_projects() == []
        assert tools.syncs == [1]

    def test_delete_unknown_project(self, tools):
        assert tools.delete_project("ghost") == {"status": "not_found", "project_id": "ghost"}
        assert tools.syncs == []

    @pytest.mark.parametrize("reserved", ["project-dev", "project-researcher", "oap"])
    def test_delete_refuses_built_in_specialists(self, tools, reserved):
        assert tools.delete_project(reserved)["status"] == "error"


class TestAgentRegistryTools:
    def test_list_registered_agents_reports_groups(self, tools):
        upsert_spec(spec())
        upsert_project(project())
        result = tools.list_registered_agents()
        assert result["status"] == "ok"
        assert result["core_agents"] == sorted(RESERVED_AGENT_IDS)
        assert result["platform_agents"] == sorted(PLATFORM_AGENT_IDS)
        assert result["project_templates"] == sorted(PROJECT_TEMPLATE_IDS)
        assert [a["agent_id"] for a in result["custom_agents"]] == ["research"]
        assert [p["project_id"] for p in result["projects"]] == ["acme"]

    @pytest.mark.parametrize("reserved", ["platform-skills", "platform-scheduler", "project-dev"])
    def test_create_agent_rejects_new_reserved_ids(self, tools, reserved):
        result = tools.create_or_update_agent(agent_id=reserved, name="x")
        assert result["status"] == "error"
        assert load_specs() == []

    def test_create_agent_rejects_a_project_id(self, tools):
        upsert_project(project("acme"))
        result = tools.create_or_update_agent(agent_id="acme", name="Acme")
        assert result["status"] == "error"
        assert "RegistryError" in result["error"]
        assert load_specs() == []
        assert tools.syncs == []

    @pytest.mark.parametrize("reserved", ["platform-scheduler", "dev-code-reviewer"])
    def test_delete_custom_agent_refuses_reserved_ids(self, tools, reserved):
        assert tools.delete_custom_agent(reserved)["status"] == "error"

    def test_delete_custom_agent_is_directly_callable(self, tools):
        upsert_spec(spec())
        assert tools.delete_custom_agent("research")["status"] == "ok"
        assert load_specs() == []
