"""OpenAI-compatible `/v1` surface that lets Open WebUI talk to OAP.

Open WebUI speaks the OpenAI chat-completions protocol; OAP runs the agents.
This router is the translation layer, plus the single place where uploaded
files are routed into workspace folders.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from app.agents import runtime
from app.agents.store import bind_folder_to_agent_if_custom, resolve_agent_target_folder
from app.bridge import openwebui_sync
from app.bridge.schemas import (
    ChatCompletionRequest,
    ChatMessage,
    FileRouteRequest,
    ModelCard,
    ModelList,
    RunCancelRequest,
    RunResolveRequest,
)
from app.bridge.security import require_bridge_auth
from app.knowledge import knowledge_manager as km
from app.knowledge.file_watcher import watcher_service
from app.knowledge.paths import UnsafePathError
from app.settings import settings
from app.telemetry.otel import get_tracer

logger = logging.getLogger(__name__)
tracer = get_tracer("oap.bridge")

FOLDER_TAG = re.compile(r"\s*#folder:(\S+)\s*")
BACKGROUND_TAG = re.compile(r"(?:^|\s)#background(?=\s|$)", re.IGNORECASE)
HITL_CONFIRM_DIRECTIVES = frozenset(
    {"yes", "y", "approve", "approved", "confirm", "confirmed", "#approve", "#confirm"}
)
HITL_REJECT_DIRECTIVES = frozenset(
    {"no", "n", "reject", "rejected", "cancel", "deny", "#reject", "#cancel"}
)

router = APIRouter(prefix="/v1", tags=["open-webui"], dependencies=[Depends(require_bridge_auth)])


# --------------------------------------------------------------------------- #
# Injectable collaborators (overridden in tests)
# --------------------------------------------------------------------------- #
def get_agent_catalog() -> list[dict[str, str]]:
    return runtime.describe_agents()


def get_runner_factory() -> Callable[[str | None], Any]:
    return runtime.get_runner


# --------------------------------------------------------------------------- #
# Message helpers (pure)
# --------------------------------------------------------------------------- #
def message_text(message: ChatMessage | None) -> str:
    """Flatten OpenAI message content, which may be a string or content parts."""
    if message is None:
        return ""
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") in (None, "text")
        ]
        return "\n".join(p for p in parts if p)
    return "" if content is None else str(content)


def last_user_message(messages: list[ChatMessage]) -> str:
    for message in reversed(messages):
        if message.role == "user":
            return message_text(message)
    return message_text(messages[-1]) if messages else ""


def extract_folder_tag(prompt: str) -> tuple[str | None, str]:
    """Pull a leading/inline `#folder:<path>` tag out of the prompt."""
    match = FOLDER_TAG.search(prompt)
    if not match:
        return None, prompt
    folder = match.group(1).strip().rstrip(".,;")
    cleaned = FOLDER_TAG.sub(" ", prompt, count=1).strip()
    return (folder or None), cleaned


def extract_background_tag(prompt: str) -> tuple[bool, str]:
    """Pull a leading/inline `#background` tag out of the prompt."""
    if not BACKGROUND_TAG.search(prompt):
        return False, prompt
    cleaned = BACKGROUND_TAG.sub(" ", prompt, count=1).strip()
    return True, cleaned


def _parse_hitl_decision(prompt: str) -> Literal["confirm", "reject"] | None:
    """Detect whether a user message is a HITL confirmation or rejection directive."""
    cleaned = prompt.strip()
    if not cleaned:
        return None
    normalised = cleaned.lower().rstrip(".!,;")
    if normalised in HITL_CONFIRM_DIRECTIVES:
        return "confirm"
    if normalised in HITL_REJECT_DIRECTIVES:
        return "reject"
    first_token = normalised.split()[0].rstrip(":.,;")
    word_count = len(normalised.split())
    if first_token in ("#approve", "#confirm", "approve", "confirm") and word_count <= 6:
        return "confirm"
    if first_token in ("#reject", "#cancel", "reject", "cancel") and word_count <= 12:
        return "reject"
    return None


def upload_names(files: list[dict[str, Any]]) -> list[str]:
    """Best-effort filenames from Open WebUI's several file payload shapes."""
    names: list[str] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        candidate = (
            item.get("name")
            or (item.get("file") or {}).get("filename")
            or (item.get("file") or {}).get("name")
            or item.get("filename")
            or item.get("id")
        )
        if candidate:
            names.append(str(candidate))
    return names


def session_id_for(request: ChatCompletionRequest, model: str, user_id: str) -> str:
    return request.session_id or request.chat_id or f"owui-{model}-{user_id}"


def normalise_model_id(model: str | None) -> str:
    """Resolve an Open WebUI Workspace Model or Pipe ID (`agent.<id>`, `oap.<id>`) to its OAP id."""
    if not model or not model.strip():
        return runtime.TEAM_ID
    cleaned = model.strip()
    for prefix in ("agent.", "oap.", "agentos."):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
            break
    return cleaned or runtime.TEAM_ID


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@router.get("/files/watcher-status")
def watcher_status() -> dict[str, Any]:
    return watcher_service.status()


@router.post("/files/route")
def route_file(request: FileRouteRequest) -> dict[str, Any]:
    """Stage an Open WebUI upload into the workspace folder for an agent."""
    agent_id = normalise_model_id(request.agent_id) if request.agent_id else request.agent_id
    try:
        folder = resolve_agent_target_folder(agent_id, request.target_folder)
        result = km.stage_openwebui_upload(
            request.filename_query, folder, auto_index=request.auto_index
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (UnsafePathError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    if request.bind_to_agent:
        result["bound_to_agent"] = bind_folder_to_agent_if_custom(agent_id, folder)
    return result


@router.get("/models", response_model=ModelList)
def list_models(catalog: list[dict[str, str]] = Depends(get_agent_catalog)) -> ModelList:
    if openwebui_sync.get_last_sync_result().get("status") != "ok":
        openwebui_sync.ensure_synced_in_background()
    return ModelList(
        data=[
            ModelCard(
                id=entry["id"],
                name=entry.get("name") or entry["id"],
                group=entry.get("group"),
                kind=entry.get("kind"),
            )
            for entry in catalog
        ]
    )


@router.post("/chat/completions")
async def chat_completions(
    request: ChatCompletionRequest,
    runner_factory: Callable[[str | None], Any] = Depends(get_runner_factory),
) -> Any:
    model = normalise_model_id(request.model)
    user_id = request.user or "open-webui"
    prompt = last_user_message(request.messages)
    folder_tag, prompt = extract_folder_tag(prompt)
    bg_tag, prompt = extract_background_tag(prompt)
    is_background = bool(request.background or bg_tag)

    routed = _route_request_files(request, model, folder_tag)
    if routed:
        prompt = f"{prompt}\n\n{_routing_note(routed)}".strip()

    runner = runner_factory(model)
    if runner is None:
        if not settings.llm_configured:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "OPENROUTER_API_KEY is not configured, so no agent can run. "
                    "Set it in .env and restart the API. See /health."
                ),
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown model {model!r}"
        )

    session_id = session_id_for(request, model, user_id)

    # Check if the user is replying with a HITL confirmation/rejection to a paused run
    decision = _parse_hitl_decision(prompt)
    if decision is not None and not is_background and hasattr(runner, "aget_last_run_output"):
        try:
            last_run = await runner.aget_last_run_output(session_id=session_id)
        except Exception:
            last_run = None
        if last_run is not None and _is_run_paused(last_run):
            return await _resolve_and_continue_run(
                runner=runner,
                paused_run=last_run,
                decision=decision,
                note=prompt if decision == "reject" else None,
                model=model,
                session_id=session_id,
                user_id=user_id,
                stream=request.stream,
            )

    if is_background and not request.stream:
        return await _start_background_run(runner, prompt, model, session_id, user_id)

    if request.stream:
        return StreamingResponse(
            _stream_completion(
                runner, prompt, model, session_id, user_id, background=is_background
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return await _complete(runner, prompt, model, session_id, user_id)


@router.post("/runs/{run_id}/resolve")
async def resolve_paused_run(
    run_id: str,
    request: RunResolveRequest,
    runner_factory: Callable[[str | None], Any] = Depends(get_runner_factory),
) -> Any:
    """Confirm or reject a paused Human-in-the-Loop (HITL) run and resume execution."""
    model = normalise_model_id(request.model)
    runner = runner_factory(model)
    if runner is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown model {model!r}"
        )

    paused_run = None
    if hasattr(runner, "aget_run_output"):
        paused_run = await runner.aget_run_output(run_id=run_id, session_id=request.session_id)
    if paused_run is None and hasattr(runner, "aget_last_run_output"):
        candidate = await runner.aget_last_run_output(session_id=request.session_id)
        if candidate is not None and getattr(candidate, "run_id", run_id) == run_id:
            paused_run = candidate
    if paused_run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Run {run_id!r} not found"
        )
    if not _is_run_paused(paused_run):
        raw_status = getattr(paused_run, "status", None)
        status_str = str(getattr(raw_status, "value", raw_status) or "UNKNOWN").upper()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Run {run_id!r} is not paused (status={status_str})",
        )

    session_id = (
        request.session_id or getattr(paused_run, "session_id", None) or f"owui-{model}-resolve"
    )
    user_id = request.user or "open-webui"
    return await _resolve_and_continue_run(
        runner=runner,
        paused_run=paused_run,
        decision=request.decision,
        note=request.note,
        model=model,
        session_id=session_id,
        user_id=user_id,
        stream=request.stream,
    )


@router.get("/runs/{run_id}")
async def get_run_status(
    run_id: str,
    model: str | None = None,
    session_id: str | None = None,
    runner_factory: Callable[[str | None], Any] = Depends(get_runner_factory),
) -> dict[str, Any]:
    """Poll the status and output of a background or paused run."""
    target_model = normalise_model_id(model)
    runner = runner_factory(target_model)
    if runner is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown model {target_model!r}"
        )
    if not hasattr(runner, "aget_run_output"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Run {run_id!r} not found"
        )
    result = await runner.aget_run_output(run_id=run_id, session_id=session_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Run {run_id!r} not found"
        )
    raw_status = getattr(result, "status", None)
    status_str = str(getattr(raw_status, "value", raw_status) or "UNKNOWN").upper()
    reqs = _active_requirements(result)
    return {
        "run_id": getattr(result, "run_id", None) or run_id,
        "model": target_model,
        "session_id": getattr(result, "session_id", None) or session_id,
        "status": status_str,
        "content": getattr(result, "content", None),
        "is_paused": _is_run_paused(result),
        "active_requirements": _serialise_requirements(reqs),
    }


@router.post("/runs/{run_id}/cancel")
async def cancel_run_endpoint(
    run_id: str,
    request: RunCancelRequest | None = None,
    model: str | None = None,
    runner_factory: Callable[[str | None], Any] = Depends(get_runner_factory),
) -> dict[str, Any]:
    """Cancel an in-flight or background run."""
    target_model = normalise_model_id((request.model if request else None) or model)
    runner = runner_factory(target_model)
    if runner is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown model {target_model!r}"
        )
    if hasattr(runner, "acancel_run"):
        cancelled = await runner.acancel_run(run_id=run_id)
    elif hasattr(runner, "cancel_run"):
        cancelled = runner.cancel_run(run_id=run_id)
    else:
        cancelled = False
    return {
        "run_id": run_id,
        "model": target_model,
        "status": "cancelled" if cancelled else "not_found",
        "cancelled": bool(cancelled),
    }


def _route_request_files(
    request: ChatCompletionRequest, model: str, folder_tag: str | None
) -> list[dict[str, Any]]:
    """Stage any files attached to this message. Idempotent by content hash."""
    names = upload_names(request.files)
    if not names:
        return []
    results: list[dict[str, Any]] = []
    for name in names:
        try:
            folder = resolve_agent_target_folder(model, folder_tag)
            results.append(km.stage_openwebui_upload(name, folder, auto_index=True))
        except Exception as exc:
            logger.warning("Could not route upload %s: %s", name, exc)
            results.append({"source": name, "error": str(exc)})
    return results


def _routing_note(results: list[dict[str, Any]]) -> str:
    lines = ["[platform] Attached files were handled as follows:"]
    for item in results:
        if "error" in item:
            lines.append(f"- {item.get('source')}: FAILED ({item['error']})")
            continue
        state = "indexed" if item.get("indexed") else "stored (not indexed)"
        lines.append(
            f"- {item['saved_to']} -> folder '{item['folder']}', "
            f"table '{item['pgvector_table']}', {state}"
        )
    lines.append("Search your knowledge base before answering questions about them.")
    return "\n".join(lines)


def _chunk(model: str, completion_id: str, delta: dict[str, Any], finish: str | None = None) -> str:
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload)}\n\n"


def _event_text(event: Any) -> str | None:
    if type(event).__name__ != "RunContentEvent":
        return None
    content = getattr(event, "content", None)
    return content if isinstance(content, str) and content else None


def _event_error(event: Any) -> str | None:
    """Message from a failed run event, if this event is one."""
    if type(event).__name__ != "RunErrorEvent":
        return None
    return str(getattr(event, "content", None) or "the agent run failed")


def _event_paused_text(event: Any) -> str | None:
    """Formatted HITL confirmation prompt when a streaming run pauses."""
    if type(event).__name__ != "RunPausedEvent":
        return None
    run_id = getattr(event, "run_id", None)
    reqs = list(
        getattr(event, "requirements", None)
        or getattr(event, "active_requirements", None)
        or []
    )
    return _format_paused_requirements(run_id, reqs)


def _is_run_paused(result: Any) -> bool:
    if bool(getattr(result, "is_paused", False)):
        return True
    raw_status = getattr(result, "status", None)
    return str(getattr(raw_status, "value", raw_status) or "").upper() == "PAUSED"


def _active_requirements(result: Any) -> list[Any]:
    return list(
        getattr(result, "active_requirements", None)
        or getattr(result, "requirements", None)
        or []
    )


def _serialise_requirements(requirements: list[Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for req in requirements:
        tool_exec = getattr(req, "tool_execution", None)
        source = tool_exec if tool_exec else req
        items.append(
            {
                "tool_name": getattr(source, "tool_name", None),
                "tool_args": getattr(source, "tool_args", None),
                "needs_confirmation": bool(getattr(req, "needs_confirmation", True)),
                "member_agent_name": getattr(req, "member_agent_name", None),
            }
        )
    return items


def _format_paused_requirements(run_id: str | None, requirements: list[Any]) -> str:
    lines = [
        "### Human-in-the-Loop Confirmation Required",
        "",
        f"Run `{run_id or 'unknown'}` is paused awaiting confirmation before executing:",
    ]
    for req in requirements:
        tool_exec = getattr(req, "tool_execution", None)
        tool_name = (
            getattr(tool_exec, "tool_name", None)
            if tool_exec is not None
            else getattr(req, "tool_name", None)
        ) or "tool"
        tool_args = (
            getattr(tool_exec, "tool_args", None)
            if tool_exec is not None
            else getattr(req, "tool_args", None)
        ) or {}
        member_name = getattr(req, "member_agent_name", None)
        prefix = f"[{member_name}] " if member_name else ""
        lines.append(f"- {prefix}`{tool_name}({ json.dumps(tool_args, sort_keys=True) })`")
    lines.extend(
        [
            "",
            "Reply **approve** (or **yes**) to confirm and continue, "
            "or **reject** (or **no**) to cancel.",
            "You can also resolve programmatically via "
            f"`POST /v1/runs/{run_id or '<run_id>'}/resolve`.",
        ]
    )
    return "\n".join(lines)


def run_failed(result: Any) -> bool:
    """True when Agno finished the run in its error state.

    Agno does not raise on a provider failure: it completes the run with
    `status=RunStatus.error` and puts the provider's message in `content`.
    Without this check an invalid API key reaches the user as a perfectly
    ordinary-looking assistant reply saying "User not found."
    """
    status = getattr(result, "status", None)
    return str(getattr(status, "value", status) or "").upper() == "ERROR"


async def _stream_events(
    event_stream: AsyncIterator[Any],
    model: str,
    completion_id: str,
    span: Any,
) -> AsyncIterator[str]:
    async for event in event_stream:
        text = _event_text(event)
        if text:
            yield _chunk(model, completion_id, {"content": text})
            continue
        paused_text = _event_paused_text(event)
        if paused_text:
            span.set_attribute("oap.hitl_paused", True)
            yield _chunk(model, completion_id, {"content": f"\n\n{paused_text}"})
            continue
        failure = _event_error(event)
        if failure:
            logger.error("Run failed for model %s: %s", model, failure)
            span.set_attribute("oap.run_failed", True)
            yield _chunk(model, completion_id, {"content": f"\n\n[oap error] {failure}"})


async def _stream_completion(
    runner: Any,
    prompt: str,
    model: str,
    session_id: str,
    user_id: str,
    *,
    background: bool = False,
) -> AsyncIterator[str]:
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    yield _chunk(model, completion_id, {"role": "assistant", "content": ""})
    with tracer.start_as_current_span("openwebui.chat_stream") as span:
        span.set_attribute("oap.model", model)
        span.set_attribute("oap.session_id", session_id)
        try:
            kwargs: dict[str, Any] = {
                "stream": True,
                "session_id": session_id,
                "user_id": user_id,
            }
            if background:
                kwargs["background"] = True
            stream_iter = runner.arun(prompt, **kwargs)
            async for chunk in _stream_events(stream_iter, model, completion_id, span):
                yield chunk
        except Exception as exc:
            logger.exception("Streaming run failed for model %s", model)
            span.record_exception(exc)
            yield _chunk(model, completion_id, {"content": f"\n\n[oap error] {exc}"})
    yield _chunk(model, completion_id, {}, finish="stop")
    yield "data: [DONE]\n\n"


def _format_completion_response(result: Any, model: str) -> dict[str, Any]:
    if _is_run_paused(result):
        run_id = getattr(result, "run_id", None)
        reqs = _active_requirements(result)
        content = _format_paused_requirements(run_id, reqs)
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "hitl_paused": True,
            "run_id": run_id,
            "active_requirements": _serialise_requirements(reqs),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    content = getattr(result, "content", None)
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content if content is not None else ""},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


async def _complete(
    runner: Any, prompt: str, model: str, session_id: str, user_id: str
) -> dict[str, Any]:
    with tracer.start_as_current_span("openwebui.chat_completion") as span:
        span.set_attribute("oap.model", model)
        span.set_attribute("oap.session_id", session_id)
        try:
            result = await runner.arun(prompt, session_id=session_id, user_id=user_id)
        except Exception as exc:
            logger.exception("Run failed for model %s", model)
            span.record_exception(exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Agent run failed: {exc}"
            ) from exc

        if run_failed(result):
            message = getattr(result, "content", None) or "the agent run failed"
            logger.error("Run failed for model %s: %s", model, message)
            span.set_attribute("oap.run_failed", True)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Agent run failed: {message}"
            )

        if _is_run_paused(result):
            span.set_attribute("oap.hitl_paused", True)

    return _format_completion_response(result, model)


async def _start_background_run(
    runner: Any, prompt: str, model: str, session_id: str, user_id: str
) -> dict[str, Any]:
    with tracer.start_as_current_span("openwebui.background_run") as span:
        span.set_attribute("oap.model", model)
        span.set_attribute("oap.session_id", session_id)
        span.set_attribute("oap.background", True)
        try:
            result = await runner.arun(
                prompt, session_id=session_id, user_id=user_id, background=True
            )
        except Exception as exc:
            logger.exception("Background run start failed for model %s", model)
            span.record_exception(exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Background run failed: {exc}"
            ) from exc

    run_id = getattr(result, "run_id", None) or f"bg-{uuid.uuid4().hex[:12]}"
    raw_status = getattr(result, "status", None)
    status_str = str(getattr(raw_status, "value", raw_status) or "PENDING").upper()
    content = (
        f"Background run `{run_id}` accepted "
        f"(`status={status_str}`, `session_id={session_id}`).\n\n"
        f"Poll `GET /v1/runs/{run_id}?model={model}&session_id={session_id}` "
        f"or ask `platform-manager` to check background run `{run_id}`."
    )
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "run_id": run_id,
        "session_id": session_id,
        "status": status_str,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


async def _resolve_and_continue_run(
    *,
    runner: Any,
    paused_run: Any,
    decision: Literal["confirm", "reject"],
    note: str | None,
    model: str,
    session_id: str,
    user_id: str,
    stream: bool,
) -> Any:
    active_reqs = _active_requirements(paused_run)
    all_reqs = list(getattr(paused_run, "requirements", None) or active_reqs)
    for req in active_reqs or all_reqs:
        if decision == "confirm" and hasattr(req, "confirm"):
            req.confirm()
        elif decision == "reject" and hasattr(req, "reject"):
            req.reject(note=note)

    run_id = getattr(paused_run, "run_id", None)
    if stream:
        async def _continue_stream() -> AsyncIterator[str]:
            completion_id = f"chatcmpl-{uuid.uuid4().hex}"
            yield _chunk(model, completion_id, {"role": "assistant", "content": ""})
            with tracer.start_as_current_span("openwebui.hitl_continue_stream") as span:
                span.set_attribute("oap.model", model)
                span.set_attribute("oap.run_id", str(run_id or ""))
                span.set_attribute("oap.hitl_decision", decision)
                try:
                    stream_iter = runner.acontinue_run(
                        run_id=run_id,
                        requirements=all_reqs,
                        session_id=session_id,
                        user_id=user_id,
                        stream=True,
                    )
                    async for chunk in _stream_events(stream_iter, model, completion_id, span):
                        yield chunk
                except Exception as exc:
                    logger.exception("Streaming HITL continue failed for model %s", model)
                    span.record_exception(exc)
                    yield _chunk(model, completion_id, {"content": f"\n\n[oap error] {exc}"})
            yield _chunk(model, completion_id, {}, finish="stop")
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            _continue_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    with tracer.start_as_current_span("openwebui.hitl_continue") as span:
        span.set_attribute("oap.model", model)
        span.set_attribute("oap.run_id", str(run_id or ""))
        span.set_attribute("oap.hitl_decision", decision)
        try:
            continued = await runner.acontinue_run(
                run_id=run_id,
                requirements=all_reqs,
                session_id=session_id,
                user_id=user_id,
                stream=False,
            )
        except Exception as exc:
            logger.exception("HITL continue failed for model %s", model)
            span.record_exception(exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to continue paused run: {exc}",
            ) from exc

        if run_failed(continued):
            message = getattr(continued, "content", None) or "the continued run failed"
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Agent run failed: {message}"
            )

    return _format_completion_response(continued, model)
