"""Bridge authentication, model listing and prompt handling.

The router is mounted on a bare FastAPI app with its collaborators overridden,
so these tests need no database, no Agno models and no network.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.bridge import security
from app.bridge.openwebui_router import (
    extract_folder_tag,
    get_agent_catalog,
    get_runner_factory,
    last_user_message,
    message_text,
    router,
    run_failed,
    session_id_for,
    upload_names,
)
from app.bridge.schemas import ChatCompletionRequest, ChatMessage

CATALOG = [
    {"id": "oap", "name": "Platform Coordinator", "kind": "team"},
    {"id": "platform-builder", "name": "Platform Builder", "kind": "agent"},
]


class FakeRunOutput:
    def __init__(self, content):
        self.content = content


class FakeRunner:
    def __init__(self, content="hello"):
        self.content = content
        self.calls = []

    async def arun(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        return FakeRunOutput(self.content)


@pytest.fixture()
def api(monkeypatch):
    monkeypatch.setattr(security.settings, "os_security_key", "test-key")
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_agent_catalog] = lambda: CATALOG
    runner = FakeRunner()
    app.dependency_overrides[get_runner_factory] = lambda: lambda model: runner
    client = TestClient(app)
    client.runner = runner
    return client


AUTH = {"Authorization": "Bearer test-key"}


class TestAuth:
    def test_rejects_a_missing_key(self, api):
        response = api.get("/v1/models")
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"

    def test_rejects_a_wrong_key(self, api):
        assert api.get("/v1/models", headers={"Authorization": "Bearer nope"}).status_code == 401

    def test_rejects_a_non_bearer_scheme(self, api):
        assert api.get("/v1/models", headers={"Authorization": "Basic test-key"}).status_code == 401

    def test_rejects_an_empty_bearer(self, api):
        assert api.get("/v1/models", headers={"Authorization": "Bearer "}).status_code == 401

    def test_accepts_the_configured_key(self, api):
        assert api.get("/v1/models", headers=AUTH).status_code == 200

    def test_accepts_the_x_api_key_header(self, api):
        assert api.get("/v1/models", headers={"X-API-Key": "test-key"}).status_code == 200

    def test_protects_every_bridge_route(self, api):
        assert api.get("/v1/files/watcher-status").status_code == 401
        assert api.post("/v1/files/route", json={"filename_query": "a"}).status_code == 401
        assert api.post("/v1/chat/completions", json={"model": "oap"}).status_code == 401

    def test_open_when_no_key_is_configured(self, api, monkeypatch):
        monkeypatch.setattr(security.settings, "os_security_key", "")
        assert api.get("/v1/models").status_code == 200


class TestModels:
    def test_lists_the_catalog_in_openai_shape(self, api):
        payload = api.get("/v1/models", headers=AUTH).json()
        assert payload["object"] == "list"
        assert [m["id"] for m in payload["data"]] == ["oap", "platform-builder"]
        assert all(m["object"] == "model" for m in payload["data"])
        assert all(m["owned_by"] == "oap" for m in payload["data"])

    def test_includes_display_names(self, api):
        data = api.get("/v1/models", headers=AUTH).json()["data"]
        assert data[0]["name"] == "Platform Coordinator"


class TestChatCompletions:
    def test_returns_openai_shaped_completion(self, api):
        response = api.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={"model": "oap", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["object"] == "chat.completion"
        assert body["choices"][0]["message"] == {"role": "assistant", "content": "hello"}
        assert body["choices"][0]["finish_reason"] == "stop"

    def test_passes_session_and_user_through(self, api):
        api.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={
                "model": "oap",
                "user": "tyler",
                "chat_id": "chat-1",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        call = api.runner.calls[-1]
        assert call["session_id"] == "chat-1"
        assert call["user_id"] == "tyler"

    def test_strips_the_folder_tag_from_the_prompt(self, api):
        api.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={
                "model": "oap",
                "messages": [{"role": "user", "content": "summarise #folder:sops please"}],
            },
        )
        assert api.runner.calls[-1]["prompt"] == "summarise please"

    def test_streaming_emits_sse_and_a_done_sentinel(self, api):
        class Event:
            def __init__(self, content):
                self.content = content

        Event.__name__ = "RunContentEvent"

        class Runner:
            async def arun(self, prompt, **kwargs):
                for token in ("he", "llo"):
                    yield Event(token)

        api.app.dependency_overrides[get_runner_factory] = lambda: lambda model: Runner()
        with api.stream(
            "POST",
            "/v1/chat/completions",
            headers=AUTH,
            json={"model": "oap", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        ) as response:
            body = "".join(response.iter_text())
        assert body.rstrip().endswith("data: [DONE]")
        assert '"content": "he"' in body
        assert '"finish_reason": "stop"' in body


class TestFailedRuns:
    """Agno completes a failed run instead of raising, so the bridge must look."""

    def test_provider_failure_is_an_error_not_an_assistant_message(self, api):
        class Failed:
            status = type("S", (), {"value": "ERROR"})()
            content = "User not found."

        class Runner:
            async def arun(self, prompt, **kwargs):
                return Failed()

        api.app.dependency_overrides[get_runner_factory] = lambda: lambda model: Runner()
        response = api.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={"model": "oap", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 502
        assert "User not found." in response.json()["detail"]

    def test_streaming_surfaces_a_run_error_event(self, api):
        class ErrorEvent:
            def __init__(self, content):
                self.content = content

        ErrorEvent.__name__ = "RunErrorEvent"

        class Runner:
            async def arun(self, prompt, **kwargs):
                yield ErrorEvent("invalid api key")

        api.app.dependency_overrides[get_runner_factory] = lambda: lambda model: Runner()
        with api.stream(
            "POST",
            "/v1/chat/completions",
            headers=AUTH,
            json={"model": "oap", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        ) as response:
            body = "".join(response.iter_text())
        assert "[oap error] invalid api key" in body
        assert body.rstrip().endswith("data: [DONE]")

    @pytest.mark.parametrize(
        ("status_value", "expected"),
        [("ERROR", True), ("error", True), ("COMPLETED", False), (None, False)],
    )
    def test_run_failed_reads_the_status_field(self, status_value, expected):
        result = type("R", (), {"status": status_value, "content": "x"})()
        assert run_failed(result) is expected

    def test_run_failed_handles_an_enum_like_status(self):
        enum_like = type("S", (), {"value": "ERROR"})()
        assert run_failed(type("R", (), {"status": enum_like})()) is True

    def test_successful_run_is_not_treated_as_a_failure(self, api):
        response = api.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={"model": "oap", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 200


class TestPromptHelpers:
    def test_message_text_handles_plain_strings(self):
        assert message_text(ChatMessage(role="user", content="hi")) == "hi"

    def test_message_text_flattens_content_parts(self):
        message = ChatMessage(
            role="user",
            content=[
                {"type": "text", "text": "a"},
                {"type": "image_url"},
                {"type": "text", "text": "b"},
            ],
        )
        assert message_text(message) == "a\nb"

    def test_last_user_message_ignores_trailing_assistant_turns(self):
        messages = [
            ChatMessage(role="user", content="first"),
            ChatMessage(role="assistant", content="reply"),
            ChatMessage(role="user", content="second"),
            ChatMessage(role="assistant", content="reply2"),
        ]
        assert last_user_message(messages) == "second"

    def test_last_user_message_on_empty_history(self):
        assert last_user_message([]) == ""

    @pytest.mark.parametrize(
        ("prompt", "folder", "rest"),
        [
            ("#folder:sops summarise", "sops", "summarise"),
            ("summarise #folder:contracts/acme", "contracts/acme", "summarise"),
            ("no tag here", None, "no tag here"),
            ("trailing #folder:sops.", "sops", "trailing"),
        ],
    )
    def test_extract_folder_tag(self, prompt, folder, rest):
        assert extract_folder_tag(prompt) == (folder, rest)

    @pytest.mark.parametrize(
        "payload",
        [
            [{"name": "a.pdf"}],
            [{"file": {"filename": "a.pdf"}}],
            [{"file": {"name": "a.pdf"}}],
            [{"filename": "a.pdf"}],
        ],
    )
    def test_upload_names_handles_open_webui_shapes(self, payload):
        assert upload_names(payload) == ["a.pdf"]

    def test_upload_names_skips_unusable_entries(self):
        assert upload_names([{}, "junk", {"file": {}}]) == []

    def test_session_id_falls_back_to_a_stable_synthetic_id(self):
        request = ChatCompletionRequest(model="oap")
        assert session_id_for(request, "oap", "tyler") == "owui-oap-tyler"

    def test_session_id_prefers_an_explicit_session(self):
        request = ChatCompletionRequest(session_id="s1", chat_id="c1")
        assert session_id_for(request, "oap", "tyler") == "s1"
