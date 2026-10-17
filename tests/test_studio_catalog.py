"""Published control-plane agents join the Open WebUI catalogue."""

from app.agents import runtime


def test_catalogue_includes_published_studio_agents(monkeypatch):
    monkeypatch.setattr(runtime.settings, "openrouter_api_key", "sk-test")

    class Builder:
        name = "Platform Builder"

    monkeypatch.setattr(runtime, "get_all_agents_dict", lambda: {"platform-builder": Builder()})

    class Database:
        def list_components(self, **_kwargs):
            return (
                [
                    {"component_id": "draft-one", "name": "Draft", "current_version": None},
                    {
                        "component_id": "platform-builder",
                        "name": "Platform Builder",
                        "current_version": 1,
                    },
                    {"component_id": "test-agent", "name": "Test Agent", "current_version": 2},
                ],
                3,
            )

    monkeypatch.setattr(runtime, "get_db", lambda: Database())

    catalog = {entry["id"]: entry for entry in runtime.describe_agents()}
    assert catalog["test-agent"] == {
        "id": "test-agent",
        "name": "Test Agent",
        "kind": "studio",
    }
    assert "draft-one" not in catalog
    assert catalog["platform-builder"]["kind"] == "agent"


def test_runner_uses_a_published_studio_agent(monkeypatch):
    monkeypatch.setattr(runtime.settings, "openrouter_api_key", "sk-test")
    sentinel = object()
    monkeypatch.setattr(runtime, "get_all_agents_dict", lambda: {})
    monkeypatch.setattr(
        runtime,
        "load_published_studio_agent",
        lambda agent_id: sentinel if agent_id == "test-agent" else None,
    )

    assert runtime.get_runner("test-agent") is sentinel


def test_studio_catalogue_failure_does_not_hide_code_agents(monkeypatch):
    monkeypatch.setattr(runtime.settings, "openrouter_api_key", "sk-test")

    class Builder:
        name = "Platform Builder"

    monkeypatch.setattr(runtime, "get_all_agents_dict", lambda: {"platform-builder": Builder()})

    class Database:
        def list_components(self, **_kwargs):
            raise RuntimeError("components table missing")

    monkeypatch.setattr(runtime, "get_db", lambda: Database())

    ids = [entry["id"] for entry in runtime.describe_agents()]
    assert ids == ["agno", "platform-builder"]
