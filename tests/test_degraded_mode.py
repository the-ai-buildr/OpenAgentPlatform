"""Behaviour when no provider key is configured.

Starting the stack before pasting an OpenRouter key is the normal first
experience, so the degraded path is a supported mode rather than an edge case:
the API must answer, not crash or 500.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agents import runtime
from app.bridge import openwebui_router as bridge
from app.bridge import security
from app.bridge.openwebui_router import get_agent_catalog, get_runner_factory, router

AUTH = {"Authorization": "Bearer test-key"}


@pytest.fixture()
def no_llm(monkeypatch):
    for module in (security, bridge, runtime):
        monkeypatch.setattr(module.settings, "openrouter_api_key", "", raising=False)
    monkeypatch.setattr(security.settings, "os_security_key", "test-key")
    app = FastAPI()
    app.include_router(router)
    # Use the real collaborators: the point is that they stay well-behaved.
    app.dependency_overrides[get_agent_catalog] = runtime.describe_agents
    app.dependency_overrides[get_runner_factory] = lambda: runtime.get_runner
    return TestClient(app, raise_server_exceptions=False)


def test_models_returns_an_empty_list_rather_than_failing(no_llm):
    response = no_llm.get("/v1/models", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {"object": "list", "data": []}


def test_models_still_requires_the_key(no_llm):
    assert no_llm.get("/v1/models").status_code == 401


def test_chat_explains_the_missing_key_instead_of_500(no_llm):
    response = no_llm.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={"model": "oap", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 503
    assert "OPENROUTER_API_KEY" in response.json()["detail"]


def test_runner_resolution_returns_none(monkeypatch):
    monkeypatch.setattr(runtime.settings, "openrouter_api_key", "")
    assert runtime.get_runner("oap") is None
    assert runtime.get_runner(None) is None


def test_catalogue_is_empty(monkeypatch):
    monkeypatch.setattr(runtime.settings, "openrouter_api_key", "")
    assert runtime.describe_agents() == []
