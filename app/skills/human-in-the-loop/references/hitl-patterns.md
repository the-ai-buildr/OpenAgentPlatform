# Agno 3.1.4 Human-in-the-Loop (HITL) Patterns Reference

## 1. Tool Decorator Patterns

### Confirmation Before Execution
```python
from typing import Any
from agno.tools import tool


@tool(requires_confirmation=True)
def delete_custom_skill(skill_name: str) -> dict[str, Any]:
    """Delete a custom skill from workspace_files/skills/."""
    ...
```

### User Input Fields
```python
from typing import Any
from agno.tools import tool


@tool(
    requires_user_input=True,
    user_input_fields=["target_environment", "approval_ticket"],
)
def deploy_release(version: str, target_environment: str, approval_ticket: str) -> dict[str, Any]:
    """Deploy a release after the user supplies target_environment and approval_ticket."""
    return {
        "status": "deployed",
        "version": version,
        "environment": target_environment,
        "ticket": approval_ticket,
    }
```

### External Execution
```python
from agno.tools import tool


@tool(external_execution=True)
def execute_browser_action(selector: str, action: str) -> str:
    """Pause the run so the external client can execute the browser action."""
    return ""
```

### Governance & Audited Approvals (`@approval`)
```python
from agno.approval import approval
from agno.tools import tool


@tool(requires_confirmation=True)
@approval(type="audit")
def purge_schedule_history(schedule_id: str) -> dict[str, str]:
    """Purge schedule history with a persisted approval audit entry."""
    return {"status": "ok", "schedule_id": schedule_id}
```

---

## 2. Inspecting `run_response.is_paused` and `active_requirements`

When an `Agent` or `Team` pauses for HITL:
- `run_response.is_paused` is `True` (and `run_response.status == RunStatus.paused`).
- `run_response.requirements` contains all `RunRequirement` objects for the run.
- `run_response.active_requirements` filters to unresolved `RunRequirement` objects (`not req.is_resolved()`).

Each `RunRequirement` exposes:
- `req.id`: Unique requirement ID.
- `req.pause_type`: `"confirmation"`, `"user_input"`, `"user_feedback"`, or `"external_execution"`.
- `req.needs_confirmation`: `True` if awaiting `req.confirm()` or `req.reject(note=...)`.
- `req.needs_user_input`: `True` if awaiting `req.provide_user_input(values_dict)`.
- `req.needs_external_execution`: `True` if awaiting `req.set_external_execution_result(result_str)`.
- `req.tool_execution`: The `ToolExecution` instance containing `tool_name` and `tool_args`.
- `req.member_agent_id` / `req.member_agent_name` / `req.member_run_id`: Populated when the pause originated inside a `Team` member agent.

---

## 3. Resuming Paused Runs (`continue_run` / `acontinue_run`)

### Asynchronous Agent or Team Resumption
```python
async def resolve_and_continue(
    runner,  # Agent or Team
    session_id: str,
    user_id: str | None = None,
    approve: bool = True,
    note: str | None = None,
):
    paused_run = await runner.aget_last_run_output(session_id=session_id)
    if paused_run is None or not getattr(paused_run, "is_paused", False):
        raise ValueError("No paused run found for session")

    for requirement in paused_run.active_requirements:
        if requirement.needs_confirmation:
            if approve:
                requirement.confirm()
            else:
                requirement.reject(note=note or "Rejected by user")

    continued = await runner.acontinue_run(
        run_id=paused_run.run_id,
        requirements=paused_run.requirements,
        session_id=session_id,
        user_id=user_id,
        stream=False,
    )
    return continued
```

### Synchronous Agent Resumption
```python
run_response = agent.run("Delete the custom skill 'legacy-helper'", session_id=session_id)
if run_response.is_paused:
    for requirement in run_response.active_requirements:
        if requirement.needs_confirmation:
            requirement.confirm()
    run_response = agent.continue_run(
        run_id=run_response.run_id,
        requirements=run_response.requirements,
        session_id=session_id,
    )
```

### Supplying User Input Before Resuming
```python
if run_response.is_paused:
    for requirement in run_response.active_requirements:
        if requirement.needs_user_input:
            requirement.provide_user_input({
                "target_environment": "staging",
                "approval_ticket": "CHG-1042",
            })
    run_response = await agent.acontinue_run(
        run_id=run_response.run_id,
        requirements=run_response.requirements,
        session_id=session_id,
    )
```

---

## 4. Open Agent Platform Bridge Integration

In Open Agent Platform (`app/bridge/openwebui_router.py`):
1. **Destructive Tools Protected by HITL**:
   - `delete_custom_agent` (`app/tools/platform_tools.py`)
   - `delete_project` (`app/tools/platform_tools.py`)
   - `delete_schedule` (`app/tools/scheduler_tools.py`)
   - `delete_custom_skill` (`app/tools/skill_tools.py`)
2. **Pause Prompt**:
   - When a run pauses (`result.is_paused` or streaming `RunPausedEvent`), the bridge formats the active requirements (tool name, arguments, and `run_id`) into an assistant message asking the operator to reply `approve` / `yes` or `reject` / `no`.
3. **Conversational & REST Resolution**:
   - Replying `yes`, `approve`, `confirm`, or `#approve` in the same chat session confirms all active requirements and calls `runner.acontinue_run(...)`.
   - Replying `no`, `reject`, `cancel`, or `#reject` rejects the requirements and continues the run so the agent can acknowledge the cancellation.
   - Calling `POST /v1/runs/{run_id}/resolve` with `{"model": "...", "session_id": "...", "decision": "confirm" | "reject", "note": "..."}` resolves the paused run via REST.
