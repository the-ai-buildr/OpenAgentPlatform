"""Cron schedule management tools for the `platform-scheduler` specialist agent.

Wraps `agno.scheduler.ScheduleManager` so agents can list, inspect, create,
update, enable/disable, trigger, inspect run history, and (with HITL
confirmation) delete cron schedules stored in Postgres.

This module intentionally does NOT import `app.agents.runtime` at top level so
the dependency graph stays acyclic (`runtime` -> `scheduler_tools`). `runtime`
can inject its shared database provider via `set_db_provider()`, and this
module falls back to constructing a cached `PostgresDb` from `settings`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from typing import Any

from agno.db.postgres import PostgresDb
from agno.scheduler import ScheduleManager
from agno.tools import tool

from app.settings import settings

logger = logging.getLogger(__name__)

_db_getter: Callable[[], Any] | None = None
_fallback_db: PostgresDb | None = None
_manager_override: Any | None = None


def set_db_provider(provider: Callable[[], Any] | None) -> None:
    """Register a callable returning the shared Agno database instance."""
    global _db_getter
    _db_getter = provider


def set_schedule_manager(manager: Any | None) -> None:
    """Override the `ScheduleManager` instance (useful for isolated unit tests)."""
    global _manager_override
    _manager_override = manager


def _get_db() -> Any:
    global _fallback_db
    if _db_getter is not None:
        return _db_getter()
    if _fallback_db is None:
        _fallback_db = PostgresDb(id="oap-runtime", db_url=settings.database_url)
    return _fallback_db


def _get_schedule_manager() -> Any:
    if _manager_override is not None:
        return _manager_override
    return ScheduleManager(_get_db())


def _error(exc: Exception) -> dict[str, Any]:
    return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}


def _serialize_item(item: Any) -> dict[str, Any]:
    """Convert a `Schedule`, `ScheduleRun`, or dict into a clean dictionary."""
    if item is None:
        return {}
    if isinstance(item, dict):
        data = dict(item)
    elif hasattr(item, "to_dict") and callable(item.to_dict):
        data = dict(item.to_dict())
    elif is_dataclass(item) and not isinstance(item, type):
        data = asdict(item)
    else:
        data = {
            k: getattr(item, k)
            for k in dir(item)
            if not k.startswith("_") and not callable(getattr(item, k, None))
        }
    if "cron_expr" in data and "cron" not in data:
        data["cron"] = data["cron_expr"]
    return data


def list_schedules(enabled: bool | None = None, limit: int = 50) -> dict[str, Any]:
    """List registered cron schedules, optionally filtered by `enabled` state."""
    try:
        manager = _get_schedule_manager()
        schedules = manager.list(enabled=enabled, limit=limit)
        items = [_serialize_item(s) for s in schedules]
        return {
            "status": "ok",
            "count": len(items),
            "schedules": items,
        }
    except Exception as exc:
        return _error(exc)


def get_schedule(schedule_id: str) -> dict[str, Any]:
    """Fetch a single schedule by its `schedule_id`."""
    try:
        manager = _get_schedule_manager()
        schedule = manager.get(schedule_id)
        if schedule is None:
            return {
                "status": "not_found",
                "schedule_id": schedule_id,
                "error": f"Schedule not found: {schedule_id}",
            }
        return {
            "status": "ok",
            "schedule": _serialize_item(schedule),
        }
    except Exception as exc:
        return _error(exc)


def create_schedule(
    name: str,
    cron: str,
    endpoint: str,
    payload: dict[str, Any] | None = None,
    description: str = "",
    timezone: str = "UTC",
    max_retries: int = 0,
    retry_delay_seconds: int = 60,
    timeout_seconds: int = 3600,
    if_exists: str = "update",
) -> dict[str, Any]:
    """Create or update a cron schedule targeting an AgentOS endpoint.

    Args:
        name: Unique human-readable schedule name (e.g. `"daily-research-digest"`).
        cron: 5-field cron expression (e.g. `"0 9 * * *"`).
        endpoint: Relative AgentOS route such as `"/agents/project-researcher/runs"`
            or `"/teams/oap/runs"`.
        payload: JSON payload for the endpoint (for run routes, include `{"message": "..."}`).
        description: Optional description of the scheduled job.
        timezone: IANA timezone name (defaults to `"UTC"`).
        max_retries: Retry attempts on failure (defaults to `0`).
        retry_delay_seconds: Seconds between retries (defaults to `60`).
        timeout_seconds: Maximum run duration in seconds (defaults to `3600`).
        if_exists: Conflict policy when `name` already exists: `"update"`, `"skip"`, or `"raise"`.
    """
    try:
        manager = _get_schedule_manager()
        schedule = manager.create(
            name=name.strip(),
            cron=cron.strip(),
            endpoint=endpoint.strip(),
            description=description,
            payload=payload,
            timezone=timezone,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            retry_delay_seconds=retry_delay_seconds,
            if_exists=if_exists,
        )
        return {
            "status": "ok",
            "schedule": _serialize_item(schedule),
        }
    except Exception as exc:
        return _error(exc)


def update_schedule(
    schedule_id: str,
    cron: str | None = None,
    endpoint: str | None = None,
    payload: dict[str, Any] | None = None,
    description: str | None = None,
    timezone: str | None = None,
) -> dict[str, Any]:
    """Update fields on an existing cron schedule by `schedule_id`."""
    try:
        manager = _get_schedule_manager()
        updates: dict[str, Any] = {}
        if cron is not None:
            from agno.scheduler.cron import validate_cron_expr

            if not validate_cron_expr(cron.strip()):
                return {"status": "error", "error": f"ValueError: Invalid cron expression: {cron}"}
            updates["cron_expr"] = cron.strip()
        if endpoint is not None:
            updates["endpoint"] = endpoint.strip()
        if payload is not None:
            updates["payload"] = payload
        if description is not None:
            updates["description"] = description
        if timezone is not None:
            from agno.scheduler.cron import validate_timezone

            if not validate_timezone(timezone.strip()):
                return {"status": "error", "error": f"ValueError: Invalid timezone: {timezone}"}
            updates["timezone"] = timezone.strip()

        if not updates:
            return {"status": "error", "error": "ValueError: No fields provided to update"}

        if "cron_expr" in updates or "timezone" in updates:
            existing = manager.get(schedule_id)
            if existing is None:
                return {
                    "status": "not_found",
                    "schedule_id": schedule_id,
                    "error": f"Schedule not found: {schedule_id}",
                }
            existing_dict = _serialize_item(existing)
            eff_cron = updates.get("cron_expr") or existing_dict.get("cron_expr") or "* * * * *"
            eff_tz = updates.get("timezone") or existing_dict.get("timezone") or "UTC"
            if existing_dict.get("enabled", True):
                from agno.scheduler.cron import compute_next_run

                updates["next_run_at"] = compute_next_run(eff_cron, eff_tz)

        updated = manager.update(schedule_id, **updates)
        if updated is None:
            return {
                "status": "not_found",
                "schedule_id": schedule_id,
                "error": f"Schedule not found: {schedule_id}",
            }
        return {
            "status": "ok",
            "schedule": _serialize_item(updated),
        }
    except Exception as exc:
        return _error(exc)


def set_schedule_enabled(schedule_id: str, enabled: bool) -> dict[str, Any]:
    """Enable or disable a cron schedule by `schedule_id`."""
    try:
        manager = _get_schedule_manager()
        updated = manager.enable(schedule_id) if enabled else manager.disable(schedule_id)
        if updated is None:
            return {
                "status": "not_found",
                "schedule_id": schedule_id,
                "error": f"Schedule not found: {schedule_id}",
            }
        return {
            "status": "ok",
            "schedule_id": schedule_id,
            "enabled": bool(enabled),
            "schedule": _serialize_item(updated),
        }
    except Exception as exc:
        return _error(exc)


def trigger_schedule_now(schedule_id: str) -> dict[str, Any]:
    """Mark an enabled schedule as immediately due so the poller executes it on its next tick."""
    try:
        manager = _get_schedule_manager()
        schedule = manager.get(schedule_id)
        if schedule is None:
            return {
                "status": "not_found",
                "schedule_id": schedule_id,
                "error": f"Schedule not found: {schedule_id}",
            }
        sched_dict = _serialize_item(schedule)
        if not sched_dict.get("enabled", True):
            return {
                "status": "error",
                "schedule_id": schedule_id,
                "error": f"Schedule '{schedule_id}' is disabled. Enable it before triggering.",
            }
        now = int(time.time())
        updated = manager.update(schedule_id, next_run_at=now)
        return {
            "status": "ok",
            "triggered": True,
            "schedule_id": schedule_id,
            "next_run_at": now,
            "schedule": _serialize_item(updated or schedule),
        }
    except Exception as exc:
        return _error(exc)


def get_schedule_runs(schedule_id: str, limit: int = 20) -> dict[str, Any]:
    """Return the execution history (`ScheduleRun` records) for `schedule_id`."""
    try:
        manager = _get_schedule_manager()
        runs = manager.get_runs(schedule_id, limit=limit)
        items = [_serialize_item(r) for r in runs]
        return {
            "status": "ok",
            "schedule_id": schedule_id,
            "count": len(items),
            "runs": items,
        }
    except Exception as exc:
        return _error(exc)


def delete_schedule(schedule_id: str) -> dict[str, Any]:
    """Permanently delete a cron schedule by its `schedule_id`. Requires human confirmation."""
    try:
        manager = _get_schedule_manager()
        deleted = bool(manager.delete(schedule_id))
        return {
            "status": "ok" if deleted else "not_found",
            "deleted": deleted,
            "schedule_id": schedule_id,
        }
    except Exception as exc:
        return _error(exc)


delete_schedule.requires_confirmation = True  # type: ignore[attr-defined]

SCHEDULER_TOOLS = [
    list_schedules,
    get_schedule,
    create_schedule,
    update_schedule,
    set_schedule_enabled,
    trigger_schedule_now,
    get_schedule_runs,
    tool(requires_confirmation=True)(delete_schedule),
]
