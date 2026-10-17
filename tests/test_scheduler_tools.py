"""Cron schedule tool wrappers around Agno's `ScheduleManager`.

Most tests inject a fake manager through `set_schedule_manager`. One round-trip
test drives the real `ScheduleManager` against a throwaway SQLite database via
`set_db_provider`, so nothing here needs Postgres.
"""

from __future__ import annotations

import inspect
import time
import uuid
from typing import Any

import pytest
from agno.db.schemas.scheduler import Schedule, ScheduleRun
from agno.tools.function import Function

from app.tools import scheduler_tools as sch


class FakeScheduleManager:
    """In-memory stand-in mirroring the `ScheduleManager` method signatures."""

    def __init__(self) -> None:
        self.schedules: dict[str, Schedule] = {}
        self.runs: dict[str, list[ScheduleRun]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.create_error: Exception | None = None

    def add(self, name: str = "nightly", **kwargs: Any) -> Schedule:
        schedule = Schedule(
            id=kwargs.pop("id", f"sched-{uuid.uuid4().hex[:8]}"),
            name=name,
            cron_expr=kwargs.pop("cron_expr", "0 9 * * *"),
            endpoint=kwargs.pop("endpoint", "/agents/project-researcher/runs"),
            **kwargs,
        )
        self.schedules[schedule.id] = schedule
        return schedule

    def list(self, enabled=None, limit=100, page=1, user_id=None):
        self.calls.append(("list", {"enabled": enabled, "limit": limit}))
        items = [s for s in self.schedules.values() if enabled is None or s.enabled == enabled]
        return items[:limit]

    def get(self, schedule_id, user_id=None):
        self.calls.append(("get", {"schedule_id": schedule_id}))
        return self.schedules.get(schedule_id)

    def create(self, name, cron, endpoint, method="POST", description=None, payload=None,
               timezone="UTC", timeout_seconds=3600, max_retries=0, retry_delay_seconds=60,
               if_exists="raise", user_id=None, provenance=None, enabled_on_create=True):
        args = {
            "name": name,
            "cron": cron,
            "endpoint": endpoint,
            "description": description,
            "payload": payload,
            "timezone": timezone,
            "timeout_seconds": timeout_seconds,
            "max_retries": max_retries,
            "retry_delay_seconds": retry_delay_seconds,
            "if_exists": if_exists,
        }
        self.calls.append(("create", args))
        if self.create_error is not None:
            raise self.create_error
        return self.add(
            name=name,
            cron_expr=cron,
            endpoint=endpoint,
            description=description,
            payload=payload,
            timezone=timezone,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            retry_delay_seconds=retry_delay_seconds,
        )

    def update(self, schedule_id, user_id=None, **kwargs):
        self.calls.append(("update", {"schedule_id": schedule_id, **kwargs}))
        schedule = self.schedules.get(schedule_id)
        if schedule is None:
            return None
        for key, value in kwargs.items():
            setattr(schedule, key, value)
        return schedule

    def enable(self, schedule_id, user_id=None):
        self.calls.append(("enable", {"schedule_id": schedule_id}))
        return self.update(schedule_id, enabled=True)

    def disable(self, schedule_id, user_id=None):
        self.calls.append(("disable", {"schedule_id": schedule_id}))
        return self.update(schedule_id, enabled=False)

    def delete(self, schedule_id, user_id=None):
        self.calls.append(("delete", {"schedule_id": schedule_id}))
        return self.schedules.pop(schedule_id, None) is not None

    def get_runs(self, schedule_id, limit=20, page=1, user_id=None):
        self.calls.append(("get_runs", {"schedule_id": schedule_id, "limit": limit}))
        return self.runs.get(schedule_id, [])[:limit]

    def call_names(self) -> list[str]:
        return [name for name, _ in self.calls]


@pytest.fixture()
def manager():
    fake = FakeScheduleManager()
    sch.set_schedule_manager(fake)
    yield fake
    sch.set_schedule_manager(None)


class TestInjectionHooks:
    def test_override_is_returned_verbatim(self, manager):
        assert sch._get_schedule_manager() is manager

    def test_db_provider_feeds_a_real_schedule_manager(self):
        from agno.scheduler import ScheduleManager

        sentinel = object()
        sch.set_db_provider(lambda: sentinel)
        try:
            built = sch._get_schedule_manager()
            assert isinstance(built, ScheduleManager)
            assert built.db is sentinel
        finally:
            sch.set_db_provider(None)


class TestListAndGet:
    def test_lists_serialised_schedules_with_a_cron_alias(self, manager):
        manager.add("digest", cron_expr="0 7 * * 1", payload={"message": "go"})
        result = sch.list_schedules()
        assert result["status"] == "ok"
        assert result["count"] == 1
        item = result["schedules"][0]
        assert item["name"] == "digest"
        assert item["cron_expr"] == item["cron"] == "0 7 * * 1"
        assert item["payload"] == {"message": "go"}

    def test_passes_enabled_filter_and_limit(self, manager):
        manager.add("on", enabled=True)
        manager.add("off", enabled=False)
        result = sch.list_schedules(enabled=False, limit=5)
        assert [s["name"] for s in result["schedules"]] == ["off"]
        assert manager.calls[-1] == ("list", {"enabled": False, "limit": 5})

    def test_get_returns_one_schedule(self, manager):
        schedule = manager.add("digest")
        result = sch.get_schedule(schedule.id)
        assert result["status"] == "ok"
        assert result["schedule"]["id"] == schedule.id

    def test_get_unknown_schedule_is_not_found(self, manager):
        result = sch.get_schedule("missing")
        assert result["status"] == "not_found"
        assert result["schedule_id"] == "missing"

    def test_manager_errors_are_returned_not_raised(self, manager, monkeypatch):
        def boom(**kwargs):
            raise RuntimeError("db down")

        monkeypatch.setattr(manager, "list", boom)
        result = sch.list_schedules()
        assert result == {"status": "error", "error": "RuntimeError: db down"}


class TestCreate:
    def test_creates_with_defaults_and_strips_inputs(self, manager):
        result = sch.create_schedule(
            name="  daily-digest ",
            cron=" 0 9 * * * ",
            endpoint=" /agents/project-researcher/runs ",
            payload={"message": "Summarise arXiv"},
        )
        assert result["status"] == "ok"
        assert result["schedule"]["name"] == "daily-digest"
        assert result["schedule"]["cron"] == "0 9 * * *"
        _, args = manager.calls[-1]
        assert args == {
            "name": "daily-digest",
            "cron": "0 9 * * *",
            "endpoint": "/agents/project-researcher/runs",
            "description": "",
            "payload": {"message": "Summarise arXiv"},
            "timezone": "UTC",
            "timeout_seconds": 3600,
            "max_retries": 0,
            "retry_delay_seconds": 60,
            "if_exists": "update",
        }

    def test_forwards_retry_timeout_and_conflict_policy(self, manager):
        sch.create_schedule(
            name="weekly",
            cron="0 8 * * 1",
            endpoint="/teams/oap/runs",
            description="weekly ops review",
            timezone="America/Chicago",
            max_retries=3,
            retry_delay_seconds=30,
            timeout_seconds=600,
            if_exists="skip",
        )
        _, args = manager.calls[-1]
        assert args["timezone"] == "America/Chicago"
        assert args["max_retries"] == 3
        assert args["retry_delay_seconds"] == 30
        assert args["timeout_seconds"] == 600
        assert args["if_exists"] == "skip"
        assert args["description"] == "weekly ops review"

    def test_manager_validation_errors_are_reported(self, manager):
        manager.create_error = ValueError("Invalid cron expression: nope")
        result = sch.create_schedule(name="bad", cron="nope", endpoint="/teams/oap/runs")
        assert result["status"] == "error"
        assert "Invalid cron expression" in result["error"]


class TestUpdate:
    def test_cron_change_recomputes_next_run_for_enabled_schedules(self, manager):
        schedule = manager.add("digest", next_run_at=1)
        result = sch.update_schedule(schedule.id, cron="30 6 * * *")
        assert result["status"] == "ok"
        _, update_args = manager.calls[-1]
        assert update_args["cron_expr"] == "30 6 * * *"
        assert update_args["next_run_at"] > time.time()

    def test_disabled_schedule_keeps_next_run_unset(self, manager):
        schedule = manager.add("digest", enabled=False)
        sch.update_schedule(schedule.id, timezone="Europe/London")
        _, update_args = manager.calls[-1]
        assert update_args == {"schedule_id": schedule.id, "timezone": "Europe/London"}

    def test_updates_endpoint_payload_and_description(self, manager):
        schedule = manager.add("digest")
        result = sch.update_schedule(
            schedule.id,
            endpoint="/agents/project-dev/runs",
            payload={"message": "review"},
            description="moved",
        )
        assert result["status"] == "ok"
        assert result["schedule"]["endpoint"] == "/agents/project-dev/runs"
        assert result["schedule"]["payload"] == {"message": "review"}
        assert "get" not in manager.call_names()

    def test_invalid_cron_is_rejected_before_touching_the_manager(self, manager):
        schedule = manager.add("digest")
        result = sch.update_schedule(schedule.id, cron="not a cron")
        assert result["status"] == "error"
        assert "Invalid cron" in result["error"]
        assert "update" not in manager.call_names()

    def test_invalid_timezone_is_rejected(self, manager):
        schedule = manager.add("digest")
        result = sch.update_schedule(schedule.id, timezone="Mars/Olympus")
        assert result["status"] == "error"
        assert "Invalid timezone" in result["error"]
        assert "update" not in manager.call_names()

    def test_requires_at_least_one_field(self, manager):
        result = sch.update_schedule("anything")
        assert result["status"] == "error"
        assert "No fields" in result["error"]

    @pytest.mark.parametrize("kwargs", [{"cron": "0 1 * * *"}, {"description": "x"}])
    def test_unknown_schedule_is_not_found(self, manager, kwargs):
        assert sch.update_schedule("missing", **kwargs)["status"] == "not_found"


class TestEnableAndTrigger:
    def test_disable_then_enable(self, manager):
        schedule = manager.add("digest")
        disabled = sch.set_schedule_enabled(schedule.id, False)
        assert disabled["status"] == "ok"
        assert disabled["enabled"] is False
        assert disabled["schedule"]["enabled"] is False
        enabled = sch.set_schedule_enabled(schedule.id, True)
        assert enabled["schedule"]["enabled"] is True
        assert [n for n in manager.call_names() if n in ("enable", "disable")] == ["disable", "enable"]

    def test_set_enabled_on_unknown_schedule(self, manager):
        assert sch.set_schedule_enabled("missing", True)["status"] == "not_found"

    def test_trigger_marks_the_schedule_due_now(self, manager):
        schedule = manager.add("digest", next_run_at=9_999_999_999)
        before = int(time.time())
        result = sch.trigger_schedule_now(schedule.id)
        assert result["status"] == "ok"
        assert result["triggered"] is True
        assert before <= result["next_run_at"] <= int(time.time())
        assert manager.schedules[schedule.id].next_run_at == result["next_run_at"]

    def test_trigger_refuses_disabled_schedules(self, manager):
        schedule = manager.add("digest", enabled=False)
        result = sch.trigger_schedule_now(schedule.id)
        assert result["status"] == "error"
        assert "disabled" in result["error"]
        assert "update" not in manager.call_names()

    def test_trigger_unknown_schedule(self, manager):
        assert sch.trigger_schedule_now("missing")["status"] == "not_found"


class TestRunHistory:
    def test_returns_serialised_runs(self, manager):
        schedule = manager.add("digest")
        manager.runs[schedule.id] = [
            ScheduleRun(id="r1", schedule_id=schedule.id, status="success", run_id="run-1"),
            ScheduleRun(id="r2", schedule_id=schedule.id, status="failed", error="timeout"),
        ]
        result = sch.get_schedule_runs(schedule.id, limit=10)
        assert result["status"] == "ok"
        assert result["count"] == 2
        assert [r["status"] for r in result["runs"]] == ["success", "failed"]
        assert result["runs"][1]["error"] == "timeout"
        assert manager.calls[-1] == ("get_runs", {"schedule_id": schedule.id, "limit": 10})


class TestDeleteSchedule:
    def test_is_exported_as_a_hitl_confirmed_agno_tool(self):
        wrapper = next(
            t for t in sch.SCHEDULER_TOOLS if isinstance(t, Function) and t.name == "delete_schedule"
        )
        assert wrapper.requires_confirmation is True
        assert inspect.unwrap(wrapper.entrypoint) is sch.delete_schedule
        assert callable(sch.delete_schedule)
        assert not isinstance(sch.delete_schedule, Function)
        assert sch.delete_schedule.requires_confirmation is True

    def test_only_delete_requires_confirmation(self):
        confirmed = [
            t.name for t in sch.SCHEDULER_TOOLS if getattr(t, "requires_confirmation", False)
        ]
        assert confirmed == ["delete_schedule"]

    def test_is_directly_callable_and_deletes(self, manager):
        schedule = manager.add("digest")
        result = sch.delete_schedule(schedule.id)
        assert result == {"status": "ok", "deleted": True, "schedule_id": schedule.id}
        assert schedule.id not in manager.schedules

    def test_unknown_schedule_is_not_found(self, manager):
        result = sch.delete_schedule(schedule_id="missing")
        assert result["status"] == "not_found"
        assert result["deleted"] is False


class TestRealScheduleManagerRoundTrip:
    def test_full_lifecycle_against_sqlite(self, tmp_path):
        from agno.db.sqlite import SqliteDb

        db = SqliteDb(db_file=str(tmp_path / "schedules.db"))
        sch.set_schedule_manager(None)
        sch.set_db_provider(lambda: db)
        try:
            created = sch.create_schedule(
                name="nightly-research",
                cron="0 2 * * *",
                endpoint="/agents/project-researcher/runs",
                payload={"message": "Collect new arXiv papers"},
            )
            assert created["status"] == "ok", created
            schedule_id = created["schedule"]["id"]

            again = sch.create_schedule(
                name="nightly-research",
                cron="0 3 * * *",
                endpoint="/agents/project-researcher/runs",
            )
            assert again["status"] == "ok", again
            listed = sch.list_schedules()
            assert listed["count"] == 1
            assert listed["schedules"][0]["cron"] == "0 3 * * *"

            assert sch.set_schedule_enabled(schedule_id, False)["schedule"]["enabled"] is False
            assert sch.trigger_schedule_now(schedule_id)["status"] == "error"
            assert sch.list_schedules(enabled=True)["count"] == 0

            assert sch.set_schedule_enabled(schedule_id, True)["status"] == "ok"
            assert sch.trigger_schedule_now(schedule_id)["status"] == "ok"
            assert sch.update_schedule(schedule_id, description="nightly")["schedule"][
                "description"
            ] == "nightly"
            assert sch.get_schedule_runs(schedule_id) == {
                "status": "ok",
                "schedule_id": schedule_id,
                "count": 0,
                "runs": [],
            }

            assert sch.delete_schedule(schedule_id)["deleted"] is True
            assert sch.get_schedule(schedule_id)["status"] == "not_found"
        finally:
            sch.set_db_provider(None)
