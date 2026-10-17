"""Registry upsert, delete and folder-resolution rules."""

import json

import pytest

from app.agents.store import (
    AgentSpec,
    RegistryError,
    bind_folder_to_agent_if_custom,
    delete_spec,
    get_spec,
    load_specs,
    resolve_agent_target_folder,
    save_specs,
    subscribe,
    upsert_spec,
)


def spec(agent_id="research", **kwargs):
    return AgentSpec(agent_id=agent_id, name=kwargs.pop("name", "Research"), **kwargs)


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

    @pytest.mark.parametrize(
        "reserved",
        [
            "platform-builder",
            "platform-engineer",
            "platform-manager",
            "platform-telemetry",
            "agno",
        ],
    )
    def test_rejects_reserved_ids(self, registry_file, reserved):
        with pytest.raises(RegistryError):
            upsert_spec(spec(agent_id=reserved))

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

    def test_core_agents_default_to_general(self, registry_file):
        assert resolve_agent_target_folder("platform-builder") == "general"
        assert resolve_agent_target_folder("agno") == "general"

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

    def test_ignores_core_agents(self, registry_file):
        assert bind_folder_to_agent_if_custom("platform-builder", "sops") is False

    def test_ignores_unknown_agents(self, registry_file):
        assert bind_folder_to_agent_if_custom("ghost", "sops") is False


class TestSubscribers:
    def test_mutations_notify_listeners(self, registry_file):
        calls = []
        subscribe(lambda: calls.append(1))
        upsert_spec(spec())
        delete_spec("research")
        assert len(calls) == 2
