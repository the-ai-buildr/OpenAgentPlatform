---
name: human-in-the-loop
description: Design and implement Human-in-the-Loop (HITL) approval gates, user input collection, and external tool execution in Agno 3.1.4 agents and teams. Use when adding destructive or high-consequence tools, pausing runs for operator confirmation, or resuming paused agent/team runs.
---

# Human-in-the-Loop (HITL) Engineering

## Overview

Human-in-the-Loop (HITL) lets an Agno `Agent` or `Team` pause mid-run before executing a sensitive tool call, yield structured requirements (`RunRequirement`) to the caller or UI, and resume cleanly once a human confirms, rejects, supplies input fields, or provides an external tool result.

Consult `references/hitl-patterns.md` via `get_skill_reference("human-in-the-loop", "hitl-patterns.md")` for complete code templates and Open Agent Platform bridge flows.

## When to Require Human-in-the-Loop

Always gate a tool with HITL when the operation:
1. **Mutates or deletes durable state** (deleting agents, projects, skills, cron schedules, database tables, or files).
2. **Triggers external side effects** (sending messages, modifying external APIs, triggering paid jobs).
3. **Needs missing user-specific parameters** that the LLM must not guess (`requires_user_input=True` with `user_input_fields`).
4. **Executes outside the agent runtime** (`external_execution=True`), such as client-side browser or IDE actions.

> **Important constraint in Agno 3.1.4:** Only **one** of `requires_confirmation=True`, `requires_user_input=True`, or `external_execution=True` may be set to `True` on a single `@tool(...)` decorator at the same time.

## Core Agno 3.1.4 HITL Mechanisms

### 1. Tool Confirmation (`@tool(requires_confirmation=True)`)

Decorate any destructive or state-mutating function with `@tool(requires_confirmation=True)`:

```python
from agno.tools import tool

@tool(requires_confirmation=True)
def delete_custom_skill(skill_name: str) -> dict:
    """Delete a custom workspace skill after human confirmation."""
    ...
```

When the model calls `delete_custom_skill`, Agno halts execution before calling the Python function and sets `run_response.is_paused = True`.

### 2. User Input Collection (`requires_user_input=True`, `user_input_fields`)

Use `requires_user_input=True` and `user_input_fields=[...]` when the model can fill some arguments while the human must provide others:

```python
from agno.tools import tool

@tool(requires_user_input=True, user_input_fields=["api_token"])
def configure_webhook(endpoint_url: str, api_token: str) -> dict:
    """Configure a webhook; the user must supply api_token at runtime."""
    ...
```

### 3. External Tool Execution (`external_execution=True`)

Use `external_execution=True` when the caller/environment executes the tool and injects the result back into the paused run:

```python
from agno.tools import tool

@tool(external_execution=True)
def run_client_side_query(sql: str) -> str:
    """Executed by the client environment outside the agent process."""
    ...
```

### 4. Persistent Approval Records (`@approval`)

Combine `@approval` (from `agno.approval`) with `@tool` to persist approval records in the database for governance or audit trails:
- `@approval` (default `type="required"`): Requires approval and logs the decision. If no HITL flag is set on `@tool`, it automatically enables `requires_confirmation=True`.
- `@approval(type="audit")`: Logs an audit record after resolution; must be paired with `requires_confirmation=True`, `requires_user_input=True`, or `external_execution=True` on `@tool(...)`.

```python
from agno.approval import approval
from agno.tools import tool

@tool(requires_confirmation=True)
@approval(type="audit")
def drop_vector_index(table_name: str) -> dict:
    """Drop a vector table with audited confirmation."""
    ...
```

## Handling Paused Runs & Resuming Execution

After running an agent or team (`run_response = await runner.arun(...)`), always check `run_response.is_paused`:

1. **Inspect active requirements**:
   Iterate over `run_response.active_requirements` (each item is a `RunRequirement` wrapping `requirement.tool_execution`).
2. **Resolve each requirement**:
   - **Confirmation**:
     - Approve: `requirement.confirm()`
     - Reject: `requirement.reject(note="Optional rejection reason")`
   - **User Input**:
     - `requirement.provide_user_input({"api_token": "..."})`
   - **External Execution**:
     - `requirement.set_external_execution_result(json_or_text_result)`
3. **Continue the run**:
   - Synchronous Agent/Team:
     `agent.continue_run(run_id=run_response.run_id, requirements=run_response.requirements, session_id=session_id)`
   - Asynchronous Agent/Team:
     `await agent.acontinue_run(run_id=run_response.run_id, requirements=run_response.requirements, session_id=session_id, user_id=user_id, stream=False)`
     or `await team.acontinue_run(run_id=run_response.run_id, requirements=run_response.requirements, session_id=session_id, user_id=user_id, stream=False)`

## Verification Checklist

- [ ] Every destructive tool is decorated with `@tool(requires_confirmation=True)`.
- [ ] Only one HITL mode flag (`requires_confirmation`, `requires_user_input`, `external_execution`) is set per tool.
- [ ] Both streaming (`RunPausedEvent`) and non-streaming (`run_response.is_paused`) paths surface `run_id`, tool name, and arguments clearly to the user.
- [ ] Resumption passes `run_id=paused_run.run_id`, `requirements=paused_run.requirements`, and the same `session_id` to `continue_run` / `acontinue_run`.
