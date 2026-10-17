"""Read-only access to the Phoenix trace store for the telemetry agent.

Phoenix serves the same data the OpenTelemetry exporter writes. These tools
call its REST API on the collector base URL (`OTEL_EXPORTER_OTLP_ENDPOINT`).
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.settings import settings

logger = logging.getLogger(__name__)

_ATTRIBUTE_KEYS = (
    "openinference.span.kind",
    "llm.model_name",
    "input.value",
    "output.value",
    "exception.message",
    "db.operation",
    "db.statement",
)
_TEXT_LIMIT = 400


def phoenix_base_url() -> str:
    return settings.otel_exporter_otlp_endpoint.rstrip("/")


def summarize_span(span: dict[str, Any]) -> dict[str, Any]:
    """Keep the fields an agent can reason about and drop bulky payloads."""
    attributes = span.get("attributes") if isinstance(span.get("attributes"), dict) else {}
    kept: dict[str, Any] = {}
    for key in _ATTRIBUTE_KEYS:
        if key not in attributes:
            continue
        value = attributes[key]
        if isinstance(value, str) and len(value) > _TEXT_LIMIT:
            value = value[:_TEXT_LIMIT] + "…"
        kept[key] = value
    context = span.get("context") if isinstance(span.get("context"), dict) else {}
    return {
        "id": span.get("id"),
        "name": span.get("name"),
        "span_kind": span.get("span_kind"),
        "status_code": span.get("status_code"),
        "status_message": span.get("status_message") or "",
        "start_time": span.get("start_time"),
        "end_time": span.get("end_time"),
        "trace_id": context.get("trace_id"),
        "attributes": kept,
    }


def summarize_trace(trace: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": trace.get("id"),
        "trace_id": trace.get("trace_id"),
        "start_time": trace.get("start_time"),
        "end_time": trace.get("end_time"),
    }


def _get_json(path: str) -> Any:
    request = Request(
        phoenix_base_url() + path,
        headers={"Accept": "application/json"},
    )
    with urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode())


def _error(message: str) -> dict[str, Any]:
    return {"status": "error", "error": message, "base_url": phoenix_base_url()}


def _fail(exc: Exception) -> dict[str, Any]:
    logger.warning("Phoenix query failed: %s", exc)
    return _error(f"{type(exc).__name__}: {exc}")


def list_phoenix_projects() -> dict[str, Any]:
    """List Phoenix projects that hold exported traces."""
    try:
        payload = _get_json("/v1/projects")
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        return _fail(exc)
    projects = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(projects, list):
        return _error("Phoenix returned no project list")
    return {
        "status": "ok",
        "base_url": phoenix_base_url(),
        "projects": [
            {"id": item.get("id"), "name": item.get("name"), "description": item.get("description")}
            for item in projects
            if isinstance(item, dict)
        ],
    }


def _project_id(project_name: str) -> tuple[str | None, dict[str, Any] | None]:
    listing = list_phoenix_projects()
    if listing.get("status") != "ok":
        return None, listing
    projects = listing["projects"]
    match = next((item for item in projects if item.get("name") == project_name), None)
    if match is None and projects:
        match = projects[0]
    if not match or not match.get("id"):
        return None, _error(f"No Phoenix project named {project_name!r}")
    return str(match["id"]), None


def list_recent_traces(project_name: str = "default", limit: int = 10) -> dict[str, Any]:
    """Recent traces in a Phoenix project. Defaults to the `default` project."""
    limit = max(1, min(int(limit), 50))
    project_id, error = _project_id(project_name)
    if error:
        return error
    try:
        payload = _get_json(f"/v1/projects/{project_id}/traces?limit={limit}")
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        return _fail(exc)
    traces = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(traces, list):
        return _error("Phoenix returned no traces")
    return {
        "status": "ok",
        "project_name": project_name,
        "traces": [summarize_trace(item) for item in traces if isinstance(item, dict)],
    }


def list_recent_spans(project_name: str = "default", limit: int = 15) -> dict[str, Any]:
    """Recent spans in a Phoenix project, trimmed to names, status and a few attributes."""
    limit = max(1, min(int(limit), 50))
    project_id, error = _project_id(project_name)
    if error:
        return error
    try:
        payload = _get_json(f"/v1/projects/{project_id}/spans?limit={limit}")
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        return _fail(exc)
    spans = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(spans, list):
        return _error("Phoenix returned no spans")
    return {
        "status": "ok",
        "project_name": project_name,
        "spans": [summarize_span(item) for item in spans if isinstance(item, dict)],
    }
