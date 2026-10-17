"""Human-in-the-Loop confirmation and background execution through the bridge.

The router is mounted on a bare FastAPI app with a fake runner injected via
`get_runner_factory`. Run outputs, requirements and pause events are real Agno
objects so the bridge sees the same shapes it gets in production; nothing here
calls an LLM, Postgres or the network.
"""

from __future__ import annotations

import inspect
import json
from typing import Any

import pytest
from agno.models.response import ToolExecution
from agno.run.agent import RunContentEvent, RunOutput, RunPausedEvent
from agno.run.base import RunStatus
from agno.run.requirement import RunRequirement
from agno.run.team import RunPausedEvent as TeamRunPausedEvent
from agno.run.team import TeamRunOutput
from agno.tools.function import Function
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.bridge import security
from app.bridge.openwebui_router import (
    _parse_hitl_decision,
    extract_background_tag,
    get_runner_factory,
    router,
)

AUTH = {"Authorization": "Bearer test-key"}


def requirement(tool_name: str = "delete_schedule", member: str | None = None, **tool_args: Any):
    req = RunRequirement(
        tool_execution=ToolExecution(
            tool_name=tool_name,
            tool_args=tool_args or {"schedule_id": "sched-1"},
            requires_confirmation=True,
        )
    )
    if member:
        req.member_agent_name = member
    return req


def paused_run(*reqs: RunRequirement, run_id: str = "run-1", session_id: str = "chat-1") -> RunOutput:
    return RunOutput(
        run_id=run_id,
        session_id=session_id,
        status=RunStatus.paused,
        requirements=list(reqs) or [requirement()],
        content="paused",
    )


def completed(content: str = "done", run_id: str = "run-1", status: RunStatus = RunStatus.completed):
    return RunOutput(run_id=run_id, session_id="chat-1", status=status, content=content)


async def _value(value: Any) -> Any:
    if isinstance(value, BaseException):
        raise value
    return value


async def _aiter(items: list[Any]):
    for item in items:
        if isinstance(item, BaseException):
            raise item
        yield item


class FakeRunner:
    """Mimics the Agno Agent/Team surface the bridge depends on."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.arun_result: Any = completed("hello")
        self.stream_events: list[Any] = [RunContentEvent(content="hello")]
        self.last_run: Any = None
        self.runs: dict[str, Any] = {}
        self.continue_result: Any = completed("schedule deleted")
        self.continue_events: list[Any] = [RunContentEvent(content="schedule deleted")]
        self.cancel_result: Any = True

    def arun(self, prompt, **kwargs):
        self.calls.append(("arun", {"prompt": prompt, **kwargs}))
        if kwargs.get("stream"):
            return _aiter(self.stream_events)
        return _value(self.arun_result)

    def acontinue_run(self, **kwargs):
        self.calls.append(("acontinue_run", kwargs))
        if kwargs.get("stream"):
            return _aiter(self.continue_events)
        return _value(self.continue_result)

    async def aget_last_run_output(self, session_id=None):
        self.calls.append(("aget_last_run_output", {"session_id": session_id}))
        return await _value(self.last_run)

    async def aget_run_output(self, run_id, session_id=None, user_id=None):
        self.calls.append(("aget_run_output", {"run_id": run_id, "session_id": session_id}))
        return self.runs.get(run_id)

    async def acancel_run(self, run_id):
        self.calls.append(("acancel_run", {"run_id": run_id}))
        return self.cancel_result

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def last(self, name: str) -> dict[str, Any]:
        return [kwargs for n, kwargs in self.calls if n == name][-1]


@pytest.fixture()
def api(monkeypatch):
    monkeypatch.setattr(security.settings, "os_security_key", "test-key")
    app = FastAPI()
    app.include_router(router)
    runner = FakeRunner()
    seen_models: list[str | None] = []

    def factory(model):
        seen_models.append(model)
        return runner

    app.dependency_overrides[get_runner_factory] = lambda: factory
    client = TestClient(app)
    client.runner = runner
    client.seen_models = seen_models
    return client


def chat(api, content: str, **extra: Any):
    payload = {
        "model": "agent.platform-scheduler",
        "chat_id": "chat-1",
        "user": "tyler",
        "messages": [{"role": "user", "content": content}],
        **extra,
    }
    return api.post("/v1/chat/completions", headers=AUTH, json=payload)


def stream_body(api, path: str, payload: dict[str, Any]) -> str:
    with api.stream("POST", path, headers=AUTH, json=payload) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        return "".join(response.iter_text())


def sse_content(body: str) -> str:
    text = []
    for line in body.splitlines():
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        delta = json.loads(line[len("data: ") :])["choices"][0]["delta"]
        text.append(delta.get("content") or "")
    return "".join(text)


# --------------------------------------------------------------------------- #
# Pause detection
# --------------------------------------------------------------------------- #
class TestPausedRunFormatting:
    def test_non_streaming_pause_returns_a_confirmation_prompt(self, api):
        api.runner.arun_result = paused_run(requirement(schedule_id="sched-42"))
        response = chat(api, "delete the nightly schedule")
        assert response.status_code == 200
        body = response.json()
        assert body["hitl_paused"] is True
        assert body["run_id"] == "run-1"
        assert body["active_requirements"] == [
            {
                "tool_name": "delete_schedule",
                "tool_args": {"schedule_id": "sched-42"},
                "needs_confirmation": True,
                "member_agent_name": None,
            }
        ]
        content = body["choices"][0]["message"]["content"]
        assert "Human-in-the-Loop Confirmation Required" in content
        assert "Run `run-1` is paused" in content
        assert '`delete_schedule({"schedule_id": "sched-42"})`' in content
        assert "**approve**" in content and "**reject**" in content
        assert "POST /v1/runs/run-1/resolve" in content

    def test_team_member_requirements_are_attributed(self, api):
        api.runner.arun_result = TeamRunOutput(
            run_id="team-run",
            status=RunStatus.paused,
            requirements=[requirement("delete_project", member="Platform Builder", project_id="acme")],
        )
        body = chat(api, "remove acme").json()
        assert body["hitl_paused"] is True
        assert body["active_requirements"][0]["member_agent_name"] == "Platform Builder"
        assert '[Platform Builder] `delete_project({"project_id": "acme"})`' in body["choices"][0][
            "message"
        ]["content"]

    def test_lists_every_pending_requirement(self, api):
        api.runner.arun_result = paused_run(
            requirement("delete_schedule", schedule_id="a"),
            requirement("delete_custom_skill", skill_name="old-skill"),
        )
        body = chat(api, "clean up").json()
        assert [r["tool_name"] for r in body["active_requirements"]] == [
            "delete_schedule",
            "delete_custom_skill",
        ]

    def test_completed_runs_have_no_hitl_fields(self, api):
        body = chat(api, "hi").json()
        assert "hitl_paused" not in body
        assert body["choices"][0]["message"]["content"] == "hello"

    def test_streaming_pause_event_is_rendered(self, api):
        api.runner.stream_events = [
            RunContentEvent(content="Checking schedules. "),
            RunPausedEvent(run_id="run-9", requirements=[requirement(schedule_id="s9")]),
        ]
        body = stream_body(
            api,
            "/v1/chat/completions",
            {
                "model": "platform-scheduler",
                "stream": True,
                "messages": [{"role": "user", "content": "delete s9"}],
            },
        )
        text = sse_content(body)
        assert text.startswith("Checking schedules. ")
        assert "Run `run-9` is paused" in text
        assert '`delete_schedule({"schedule_id": "s9"})`' in text
        assert '"finish_reason": "stop"' in body
        assert body.rstrip().endswith("data: [DONE]")

    def test_streaming_team_pause_event_is_rendered(self, api):
        api.runner.stream_events = [
            TeamRunPausedEvent(
                run_id="team-run",
                requirements=[requirement("delete_custom_agent", member="Builder", agent_id="x")],
            )
        ]
        body = stream_body(
            api,
            "/v1/chat/completions",
            {"model": "oap", "stream": True, "messages": [{"role": "user", "content": "rm x"}]},
        )
        text = sse_content(body)
        assert "Run `team-run` is paused" in text
        assert '[Builder] `delete_custom_agent({"agent_id": "x"})`' in text


# --------------------------------------------------------------------------- #
# Conversational approve / reject
# --------------------------------------------------------------------------- #
class TestConversationalResolution:
    def test_approve_confirms_and_continues_the_paused_run(self, api):
        req = requirement()
        api.runner.last_run = paused_run(req)
        response = chat(api, "approve")
        assert response.status_code == 200
        body = response.json()
        assert body["choices"][0]["message"]["content"] == "schedule deleted"
        assert "hitl_paused" not in body

        assert req.tool_execution.confirmed is True
        assert "arun" not in api.runner.names()
        assert api.runner.last("aget_last_run_output") == {"session_id": "chat-1"}
        cont = api.runner.last("acontinue_run")
        assert cont["run_id"] == "run-1"
        assert cont["requirements"] == [req]
        assert cont["session_id"] == "chat-1"
        assert cont["user_id"] == "tyler"
        assert cont["stream"] is False

    def test_reject_rejects_with_the_users_message_as_note(self, api):
        req = requirement()
        api.runner.last_run = paused_run(req)
        api.runner.continue_result = completed("Okay, I left the schedule alone.")
        body = chat(api, "reject: keep it running").json()
        assert body["choices"][0]["message"]["content"] == "Okay, I left the schedule alone."
        assert req.tool_execution.confirmed is False
        assert req.tool_execution.confirmation_note == "reject: keep it running"
        assert api.runner.last("acontinue_run")["requirements"] == [req]

    def test_every_pending_requirement_is_resolved(self, api):
        reqs = [requirement(schedule_id="a"), requirement(schedule_id="b")]
        api.runner.last_run = paused_run(*reqs)
        chat(api, "yes")
        assert all(r.tool_execution.confirmed is True for r in reqs)
        assert api.runner.last("acontinue_run")["requirements"] == reqs

    @pytest.mark.parametrize(
        "prompt", ["yes", "Y", "Approve.", "approved!", "confirm", "#approve", "#confirm", "approve the deletion"]
    )
    def test_confirm_directives(self, api, prompt):
        req = requirement()
        api.runner.last_run = paused_run(req)
        chat(api, prompt)
        assert req.tool_execution.confirmed is True

    @pytest.mark.parametrize(
        "prompt", ["no", "N", "reject", "cancel", "deny", "#reject", "#cancel", "No!"]
    )
    def test_reject_directives(self, api, prompt):
        req = requirement()
        api.runner.last_run = paused_run(req)
        chat(api, prompt)
        assert req.tool_execution.confirmed is False

    def test_streaming_approval_streams_the_continued_run(self, api):
        req = requirement()
        api.runner.last_run = paused_run(req)
        body = stream_body(
            api,
            "/v1/chat/completions",
            {
                "model": "platform-scheduler",
                "chat_id": "chat-1",
                "stream": True,
                "messages": [{"role": "user", "content": "yes"}],
            },
        )
        assert sse_content(body) == "schedule deleted"
        assert body.rstrip().endswith("data: [DONE]")
        assert req.tool_execution.confirmed is True
        assert api.runner.last("acontinue_run")["stream"] is True
        assert "arun" not in api.runner.names()

    def test_continued_run_that_pauses_again_asks_again(self, api):
        api.runner.last_run = paused_run(requirement())
        api.runner.continue_result = paused_run(
            requirement("delete_custom_skill", skill_name="s"), run_id="run-1"
        )
        body = chat(api, "approve").json()
        assert body["hitl_paused"] is True
        assert body["active_requirements"][0]["tool_name"] == "delete_custom_skill"

    def test_failed_continuation_is_a_bad_gateway(self, api):
        api.runner.last_run = paused_run(requirement())
        api.runner.continue_result = completed("provider exploded", status=RunStatus.error)
        response = chat(api, "approve")
        assert response.status_code == 502
        assert "provider exploded" in response.json()["detail"]

    def test_continuation_exception_is_a_bad_gateway(self, api):
        api.runner.last_run = paused_run(requirement())
        api.runner.continue_result = RuntimeError("run not found in session")
        response = chat(api, "approve")
        assert response.status_code == 502
        assert "run not found in session" in response.json()["detail"]

    def test_streaming_continuation_error_is_surfaced(self, api):
        api.runner.last_run = paused_run(requirement())
        api.runner.continue_events = [RuntimeError("stream broke")]
        body = stream_body(
            api,
            "/v1/chat/completions",
            {"model": "oap", "chat_id": "chat-1", "stream": True, "messages": [{"role": "user", "content": "yes"}]},
        )
        assert "[oap error] stream broke" in body
        assert body.rstrip().endswith("data: [DONE]")

    def test_directive_without_a_paused_run_is_a_normal_prompt(self, api):
        api.runner.last_run = completed("earlier answer")
        body = chat(api, "yes").json()
        assert body["choices"][0]["message"]["content"] == "hello"
        assert api.runner.last("arun")["prompt"] == "yes"
        assert "acontinue_run" not in api.runner.names()

    def test_directive_when_last_run_lookup_fails_is_a_normal_prompt(self, api):
        api.runner.last_run = RuntimeError("no session yet")
        assert chat(api, "yes").status_code == 200
        assert api.runner.last("arun")["prompt"] == "yes"

    def test_ordinary_prompt_does_not_resolve_a_paused_run(self, api):
        req = requirement()
        api.runner.last_run = paused_run(req)
        chat(api, "what schedules exist?")
        assert "aget_last_run_output" not in api.runner.names()
        assert req.tool_execution.confirmed is None
        assert api.runner.last("arun")["prompt"] == "what schedules exist?"

    def test_runner_without_last_run_lookup_runs_normally(self, api):
        class MinimalRunner:
            def __init__(self):
                self.prompts = []

            async def arun(self, prompt, **kwargs):
                self.prompts.append(prompt)
                return completed("ok")

        minimal = MinimalRunner()
        api.app.dependency_overrides[get_runner_factory] = lambda: lambda model: minimal
        assert chat(api, "approve").json()["choices"][0]["message"]["content"] == "ok"
        assert minimal.prompts == ["approve"]


# --------------------------------------------------------------------------- #
# Explicit resolve endpoint
# --------------------------------------------------------------------------- #
class TestResolveEndpoint:
    def test_confirm_continues_the_run(self, api):
        req = requirement()
        api.runner.runs["run-1"] = paused_run(req)
        response = api.post(
            "/v1/runs/run-1/resolve",
            headers=AUTH,
            json={"model": "agent.platform-scheduler", "session_id": "chat-1", "decision": "confirm"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["model"] == "platform-scheduler"
        assert body["choices"][0]["message"]["content"] == "schedule deleted"
        assert api.seen_models == ["platform-scheduler"]
        assert api.runner.last("aget_run_output") == {"run_id": "run-1", "session_id": "chat-1"}
        assert req.tool_execution.confirmed is True
        cont = api.runner.last("acontinue_run")
        assert cont["run_id"] == "run-1"
        assert cont["session_id"] == "chat-1"
        assert cont["user_id"] == "open-webui"
        assert cont["stream"] is False

    def test_reject_passes_the_note(self, api):
        req = requirement()
        api.runner.runs["run-1"] = paused_run(req)
        response = api.post(
            "/v1/runs/run-1/resolve",
            headers=AUTH,
            json={"model": "oap", "decision": "reject", "note": "too risky", "user": "ops"},
        )
        assert response.status_code == 200
        assert req.tool_execution.confirmed is False
        assert req.tool_execution.confirmation_note == "too risky"
        cont = api.runner.last("acontinue_run")
        assert cont["user_id"] == "ops"
        assert cont["session_id"] == "chat-1"

    def test_defaults_to_confirm(self, api):
        req = requirement()
        api.runner.runs["run-1"] = paused_run(req)
        api.post("/v1/runs/run-1/resolve", headers=AUTH, json={"model": "oap"})
        assert req.tool_execution.confirmed is True

    def test_falls_back_to_the_last_run_of_the_session(self, api):
        req = requirement()
        api.runner.last_run = paused_run(req, run_id="run-7")
        response = api.post(
            "/v1/runs/run-7/resolve", headers=AUTH, json={"model": "oap", "session_id": "chat-1"}
        )
        assert response.status_code == 200
        assert req.tool_execution.confirmed is True
        assert api.runner.last("acontinue_run")["run_id"] == "run-7"

    def test_last_run_with_a_different_id_is_not_used(self, api):
        api.runner.last_run = paused_run(requirement(), run_id="other-run")
        response = api.post(
            "/v1/runs/run-7/resolve", headers=AUTH, json={"model": "oap", "session_id": "chat-1"}
        )
        assert response.status_code == 404
        assert "acontinue_run" not in api.runner.names()

    def test_unknown_run_is_404(self, api):
        response = api.post("/v1/runs/nope/resolve", headers=AUTH, json={"model": "oap"})
        assert response.status_code == 404
        assert "nope" in response.json()["detail"]

    def test_run_that_is_not_paused_is_a_conflict(self, api):
        api.runner.runs["run-1"] = completed("already finished")
        response = api.post("/v1/runs/run-1/resolve", headers=AUTH, json={"model": "oap"})
        assert response.status_code == 409
        assert "not paused" in response.json()["detail"]
        assert "acontinue_run" not in api.runner.names()

    def test_unknown_model_is_404(self, api):
        api.app.dependency_overrides[get_runner_factory] = lambda: lambda model: None
        response = api.post("/v1/runs/run-1/resolve", headers=AUTH, json={"model": "ghost"})
        assert response.status_code == 404
        assert "ghost" in response.json()["detail"]

    def test_invalid_decision_is_rejected(self, api):
        response = api.post(
            "/v1/runs/run-1/resolve", headers=AUTH, json={"model": "oap", "decision": "maybe"}
        )
        assert response.status_code == 422

    def test_streaming_resolution(self, api):
        api.runner.runs["run-1"] = paused_run(requirement())
        body = stream_body(
            api,
            "/v1/runs/run-1/resolve",
            {"model": "oap", "session_id": "chat-1", "decision": "confirm", "stream": True},
        )
        assert sse_content(body) == "schedule deleted"
        assert api.runner.last("acontinue_run")["stream"] is True


# --------------------------------------------------------------------------- #
# Background execution
# --------------------------------------------------------------------------- #
class TestBackgroundRuns:
    def test_background_flag_starts_a_background_run(self, api):
        api.runner.arun_result = RunOutput(run_id="bg-1", session_id="chat-1", status=RunStatus.pending)
        response = chat(api, "Research agentic memory", background=True)
        assert response.status_code == 200
        body = response.json()
        assert body["run_id"] == "bg-1"
        assert body["session_id"] == "chat-1"
        assert body["status"] == "PENDING"
        assert body["model"] == "platform-scheduler"
        content = body["choices"][0]["message"]["content"]
        assert "Background run `bg-1` accepted" in content
        assert "GET /v1/runs/bg-1?model=platform-scheduler&session_id=chat-1" in content

        call = api.runner.last("arun")
        assert call["background"] is True
        assert call["prompt"] == "Research agentic memory"
        assert call["session_id"] == "chat-1"
        assert call["user_id"] == "tyler"
        assert "stream" not in call

    @pytest.mark.parametrize(
        "content", ["#background Research arXiv", "Research arXiv #background", "#BACKGROUND Research arXiv"]
    )
    def test_background_tag_is_stripped_and_honoured(self, api, content):
        api.runner.arun_result = RunOutput(run_id="bg-2", status=RunStatus.pending)
        body = chat(api, content).json()
        assert body["run_id"] == "bg-2"
        call = api.runner.last("arun")
        assert call["prompt"] == "Research arXiv"
        assert call["background"] is True

    def test_background_and_folder_tags_combine(self, api):
        api.runner.arun_result = RunOutput(run_id="bg-3", status=RunStatus.pending)
        chat(api, "#background summarise #folder:research the papers")
        assert api.runner.last("arun")["prompt"] == "summarise the papers"

    def test_status_reflects_the_runner(self, api):
        api.runner.arun_result = RunOutput(run_id="bg-4", status=RunStatus.running)
        assert chat(api, "go", background=True).json()["status"] == "RUNNING"

    def test_missing_run_id_gets_a_synthetic_one(self, api):
        class Accepted:
            status = None

        api.runner.arun_result = Accepted()
        body = chat(api, "go", background=True).json()
        assert body["run_id"].startswith("bg-")
        assert body["status"] == "PENDING"

    def test_start_failure_is_a_bad_gateway(self, api):
        api.runner.arun_result = ValueError("Background execution requires a database")
        response = chat(api, "go", background=True)
        assert response.status_code == 502
        assert "requires a database" in response.json()["detail"]

    def test_streaming_background_run_streams_with_background_flag(self, api):
        body = stream_body(
            api,
            "/v1/chat/completions",
            {
                "model": "oap",
                "stream": True,
                "messages": [{"role": "user", "content": "#background do the thing"}],
            },
        )
        assert sse_content(body) == "hello"
        call = api.runner.last("arun")
        assert call["stream"] is True
        assert call["background"] is True
        assert call["prompt"] == "do the thing"

    def test_foreground_streaming_does_not_pass_background(self, api):
        stream_body(
            api,
            "/v1/chat/completions",
            {"model": "oap", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        )
        assert "background" not in api.runner.last("arun")

    def test_background_approval_text_is_not_treated_as_a_decision(self, api):
        api.runner.last_run = paused_run(requirement())
        api.runner.arun_result = RunOutput(run_id="bg-5", status=RunStatus.pending)
        body = chat(api, "#background yes").json()
        assert body["run_id"] == "bg-5"
        assert "acontinue_run" not in api.runner.names()


class TestRunStatusEndpoint:
    def test_reports_a_completed_run(self, api):
        api.runner.runs["bg-1"] = RunOutput(
            run_id="bg-1", session_id="chat-1", status=RunStatus.completed, content="Report saved."
        )
        response = api.get(
            "/v1/runs/bg-1", headers=AUTH, params={"model": "agent.oap", "session_id": "chat-1"}
        )
        assert response.status_code == 200
        assert response.json() == {
            "run_id": "bg-1",
            "model": "oap",
            "session_id": "chat-1",
            "status": "COMPLETED",
            "content": "Report saved.",
            "is_paused": False,
            "active_requirements": [],
        }
        assert api.runner.last("aget_run_output") == {"run_id": "bg-1", "session_id": "chat-1"}

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (RunStatus.pending, "PENDING"),
            (RunStatus.running, "RUNNING"),
            (RunStatus.error, "ERROR"),
            (RunStatus.cancelled, "CANCELLED"),
        ],
    )
    def test_reports_lifecycle_states(self, api, status, expected):
        api.runner.runs["bg-1"] = RunOutput(run_id="bg-1", status=status)
        assert api.get("/v1/runs/bg-1", headers=AUTH).json()["status"] == expected

    def test_reports_a_paused_run_with_requirements(self, api):
        api.runner.runs["run-1"] = paused_run(requirement(schedule_id="s1"))
        body = api.get("/v1/runs/run-1", headers=AUTH, params={"model": "platform-scheduler"}).json()
        assert body["status"] == "PAUSED"
        assert body["is_paused"] is True
        assert body["active_requirements"][0]["tool_name"] == "delete_schedule"
        assert body["active_requirements"][0]["tool_args"] == {"schedule_id": "s1"}

    def test_defaults_to_the_coordinator_model(self, api):
        api.runner.runs["bg-1"] = completed(run_id="bg-1")
        api.get("/v1/runs/bg-1", headers=AUTH)
        assert api.seen_models == ["oap"]

    def test_unknown_run_is_404(self, api):
        assert api.get("/v1/runs/missing", headers=AUTH).status_code == 404

    def test_unknown_model_is_404(self, api):
        api.app.dependency_overrides[get_runner_factory] = lambda: lambda model: None
        assert api.get("/v1/runs/bg-1", headers=AUTH, params={"model": "ghost"}).status_code == 404

    def test_runner_without_run_lookup_is_404(self, api):
        api.app.dependency_overrides[get_runner_factory] = lambda: lambda model: object()
        assert api.get("/v1/runs/bg-1", headers=AUTH).status_code == 404


class TestCancelEndpoint:
    def test_cancels_with_a_json_body(self, api):
        response = api.post("/v1/runs/bg-1/cancel", headers=AUTH, json={"model": "agent.oap"})
        assert response.status_code == 200
        assert response.json() == {
            "run_id": "bg-1",
            "model": "oap",
            "status": "cancelled",
            "cancelled": True,
        }
        assert api.runner.last("acancel_run") == {"run_id": "bg-1"}

    def test_accepts_the_model_as_a_query_parameter_without_a_body(self, api):
        response = api.post("/v1/runs/bg-1/cancel", headers=AUTH, params={"model": "project-researcher"})
        assert response.status_code == 200
        assert response.json()["model"] == "project-researcher"
        assert api.seen_models == ["project-researcher"]

    def test_unknown_or_finished_run_reports_not_found(self, api):
        api.runner.cancel_result = False
        body = api.post("/v1/runs/bg-1/cancel", headers=AUTH, json={"model": "oap"}).json()
        assert body["status"] == "not_found"
        assert body["cancelled"] is False

    def test_falls_back_to_sync_cancel(self, api):
        class SyncRunner:
            cancelled: list[str] = []

            def cancel_run(self, run_id):
                self.cancelled.append(run_id)
                return True

        runner = SyncRunner()
        api.app.dependency_overrides[get_runner_factory] = lambda: lambda model: runner
        body = api.post("/v1/runs/bg-1/cancel", headers=AUTH, json={"model": "oap"}).json()
        assert body["cancelled"] is True
        assert runner.cancelled == ["bg-1"]

    def test_runner_without_cancel_support(self, api):
        api.app.dependency_overrides[get_runner_factory] = lambda: lambda model: object()
        body = api.post("/v1/runs/bg-1/cancel", headers=AUTH, json={"model": "oap"}).json()
        assert body["cancelled"] is False

    def test_unknown_model_is_404(self, api):
        api.app.dependency_overrides[get_runner_factory] = lambda: lambda model: None
        assert api.post("/v1/runs/bg-1/cancel", headers=AUTH, json={"model": "x"}).status_code == 404


class TestRunEndpointsRequireAuth:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("post", "/v1/runs/r/resolve"),
            ("get", "/v1/runs/r"),
            ("post", "/v1/runs/r/cancel"),
        ],
    )
    def test_rejects_missing_key(self, api, method, path):
        kwargs = {"json": {"model": "oap"}} if method == "post" else {}
        assert getattr(api, method)(path, **kwargs).status_code == 401


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
class TestHelpers:
    @pytest.mark.parametrize(
        ("prompt", "expected"),
        [
            ("#background research x", (True, "research x")),
            ("research x #background", (True, "research x")),
            ("#Background research", (True, "research")),
            ("research x", (False, "research x")),
            ("#backgrounds research", (False, "#backgrounds research")),
            ("foo#background bar", (False, "foo#background bar")),
        ],
    )
    def test_extract_background_tag(self, prompt, expected):
        assert extract_background_tag(prompt) == expected

    @pytest.mark.parametrize(
        ("prompt", "expected"),
        [
            ("yes", "confirm"),
            ("  Approve! ", "confirm"),
            ("#confirm", "confirm"),
            ("approve: go ahead and delete it", "confirm"),
            ("no", "reject"),
            ("Cancel.", "reject"),
            ("#reject not now", "reject"),
            ("reject because the schedule is still needed by the team", "reject"),
            ("", None),
            ("yes please explain the schedule format in a lot more detail first", None),
            ("approve this and also go ahead and create three more schedules", None),
            ("what does approve mean", None),
            ("nothing to see", None),
        ],
    )
    def test_parse_hitl_decision(self, prompt, expected):
        assert _parse_hitl_decision(prompt) == expected


# --------------------------------------------------------------------------- #
# Tool-level HITL markers and background status tool
# --------------------------------------------------------------------------- #
class TestHitlToolMarkers:
    def test_agno_sees_exactly_the_destructive_tools_as_confirmation_gated(self):
        from agno.agent import Agent
        from agno.agent._tools import parse_tools
        from agno.models.openai import OpenAIChat

        from app.tools import platform_tools, scheduler_tools, skill_tools

        model = OpenAIChat(id="test-model", api_key="not-used")
        agent = Agent(
            model=model,
            tools=[
                *platform_tools.REGISTRY_TOOLS,
                *scheduler_tools.SCHEDULER_TOOLS,
                *skill_tools.SKILL_TOOLS,
            ],
        )
        parsed = parse_tools(agent, agent.tools, model)
        gated = sorted(f.name for f in parsed if getattr(f, "requires_confirmation", False))
        assert gated == ["delete_custom_agent", "delete_custom_skill", "delete_project", "delete_schedule"]

    @pytest.mark.parametrize("name", ["delete_custom_agent", "delete_project"])
    def test_registry_wrappers_point_at_the_plain_functions(self, name):
        from app.tools import platform_tools

        wrapper = next(
            t for t in platform_tools.REGISTRY_TOOLS if isinstance(t, Function) and t.name == name
        )
        plain = getattr(platform_tools, name)
        assert wrapper.requires_confirmation is True
        assert inspect.unwrap(wrapper.entrypoint) is plain
        assert callable(plain) and not isinstance(plain, Function)
        assert plain.requires_confirmation is True


class TestCheckBackgroundRunStatusTool:
    @pytest.fixture()
    def runners(self, monkeypatch):
        from app.agents import runtime

        table: dict[str, Any] = {}
        monkeypatch.setattr(runtime, "get_runner", lambda model_id: table.get(model_id))
        return table

    def test_reports_status_of_a_background_run(self, runners):
        from app.tools.platform_tools import check_background_run_status

        class Runner:
            def get_run_output(self, run_id, session_id=None):
                assert (run_id, session_id) == ("bg-1", "chat-1")
                return RunOutput(
                    run_id="bg-1", session_id="chat-1", status=RunStatus.running, content=None
                )

        runners["project-researcher"] = Runner()
        result = check_background_run_status(" project-researcher ", "bg-1", "chat-1")
        assert result == {
            "status": "ok",
            "run_id": "bg-1",
            "agent_or_team_id": " project-researcher ",
            "session_id": "chat-1",
            "run_status": "RUNNING",
            "is_paused": False,
            "content": None,
        }

    def test_reports_paused_runs(self, runners):
        from app.tools.platform_tools import check_background_run_status

        class Runner:
            def get_run_output(self, run_id, session_id=None):
                return paused_run(requirement())

        runners["oap"] = Runner()
        result = check_background_run_status("oap", "run-1")
        assert result["run_status"] == "PAUSED"
        assert result["is_paused"] is True

    def test_unknown_agent(self, runners):
        from app.tools.platform_tools import check_background_run_status

        assert check_background_run_status("ghost", "r")["status"] == "error"

    def test_runner_without_lookup(self, runners):
        from app.tools.platform_tools import check_background_run_status

        runners["oap"] = object()
        result = check_background_run_status("oap", "r")
        assert result["status"] == "error"
        assert "does not support" in result["error"]

    def test_missing_run(self, runners):
        from app.tools.platform_tools import check_background_run_status

        class Runner:
            def get_run_output(self, run_id, session_id=None):
                return None

        runners["oap"] = Runner()
        assert check_background_run_status("oap", "r")["status"] == "not_found"

    def test_lookup_errors_are_returned(self, runners):
        from app.tools.platform_tools import check_background_run_status

        class Runner:
            def get_run_output(self, run_id, session_id=None):
                raise RuntimeError("db unavailable")

        runners["oap"] = Runner()
        assert check_background_run_status("oap", "r") == {
            "status": "error",
            "error": "RuntimeError: db unavailable",
        }

    def test_is_exposed_on_health_tools(self):
        from app.tools import platform_tools

        assert platform_tools.check_background_run_status in platform_tools.HEALTH_TOOLS
