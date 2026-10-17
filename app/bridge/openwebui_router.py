"""OpenAI-compatible `/v1` surface that lets Open WebUI talk to AgentOS.

Open WebUI speaks the OpenAI chat-completions protocol; AgentOS speaks Agno.
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
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from app.agents import runtime
from app.agents.store import bind_folder_to_agent_if_custom, resolve_agent_target_folder
from app.bridge.schemas import (
    ChatCompletionRequest,
    ChatMessage,
    FileRouteRequest,
    ModelCard,
    ModelList,
)
from app.bridge.security import require_bridge_auth
from app.knowledge import knowledge_manager as km
from app.knowledge.file_watcher import watcher_service
from app.knowledge.paths import UnsafePathError
from app.settings import settings
from app.telemetry.otel import get_tracer

logger = logging.getLogger(__name__)
tracer = get_tracer("agno.bridge")

FOLDER_TAG = re.compile(r"\s*#folder:(\S+)\s*")

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


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@router.get("/files/watcher-status")
def watcher_status() -> dict[str, Any]:
    return watcher_service.status()


@router.post("/files/route")
def route_file(request: FileRouteRequest) -> dict[str, Any]:
    """Stage an Open WebUI upload into the workspace folder for an agent."""
    try:
        folder = resolve_agent_target_folder(request.agent_id, request.target_folder)
        result = km.stage_openwebui_upload(
            request.filename_query, folder, auto_index=request.auto_index
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (UnsafePathError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    if request.bind_to_agent:
        result["bound_to_agent"] = bind_folder_to_agent_if_custom(request.agent_id, folder)
    return result


@router.get("/models", response_model=ModelList)
def list_models(catalog: list[dict[str, str]] = Depends(get_agent_catalog)) -> ModelList:
    return ModelList(
        data=[ModelCard(id=entry["id"], name=entry.get("name") or entry["id"]) for entry in catalog]
    )


@router.post("/chat/completions")
async def chat_completions(
    request: ChatCompletionRequest,
    runner_factory: Callable[[str | None], Any] = Depends(get_runner_factory),
) -> Any:
    model = request.model or runtime.TEAM_ID
    user_id = request.user or "open-webui"
    prompt = last_user_message(request.messages)
    folder_tag, prompt = extract_folder_tag(prompt)

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
    if request.stream:
        return StreamingResponse(
            _stream_completion(runner, prompt, model, session_id, user_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return await _complete(runner, prompt, model, session_id, user_id)


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


def run_failed(result: Any) -> bool:
    """True when Agno finished the run in its error state.

    Agno does not raise on a provider failure: it completes the run with
    `status=RunStatus.error` and puts the provider's message in `content`.
    Without this check an invalid API key reaches the user as a perfectly
    ordinary-looking assistant reply saying "User not found."
    """
    status = getattr(result, "status", None)
    return str(getattr(status, "value", status) or "").upper() == "ERROR"


async def _stream_completion(
    runner: Any, prompt: str, model: str, session_id: str, user_id: str
) -> AsyncIterator[str]:
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    yield _chunk(model, completion_id, {"role": "assistant", "content": ""})
    with tracer.start_as_current_span("openwebui.chat_stream") as span:
        span.set_attribute("agno.model", model)
        span.set_attribute("agno.session_id", session_id)
        try:
            async for event in runner.arun(
                prompt, stream=True, session_id=session_id, user_id=user_id
            ):
                text = _event_text(event)
                if text:
                    yield _chunk(model, completion_id, {"content": text})
                    continue
                failure = _event_error(event)
                if failure:
                    logger.error("Run failed for model %s: %s", model, failure)
                    span.set_attribute("agno.run_failed", True)
                    yield _chunk(
                        model, completion_id, {"content": f"\n\n[agentos error] {failure}"}
                    )
        except Exception as exc:
            logger.exception("Streaming run failed for model %s", model)
            span.record_exception(exc)
            yield _chunk(model, completion_id, {"content": f"\n\n[agentos error] {exc}"})
    yield _chunk(model, completion_id, {}, finish="stop")
    yield "data: [DONE]\n\n"


async def _complete(
    runner: Any, prompt: str, model: str, session_id: str, user_id: str
) -> dict[str, Any]:
    with tracer.start_as_current_span("openwebui.chat_completion") as span:
        span.set_attribute("agno.model", model)
        span.set_attribute("agno.session_id", session_id)
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
            span.set_attribute("agno.run_failed", True)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Agent run failed: {message}"
            )

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
