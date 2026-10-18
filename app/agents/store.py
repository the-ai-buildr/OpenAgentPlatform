"""Persistent registry of dynamically created agents.

This module owns the on-disk `registry.json` and the pure rules around it
(reserved ids, folder resolution, upsert semantics). It imports nothing from
Agno and nothing from `app.tools` or `app.agents.runtime`, which is what keeps
the dependency graph acyclic:

    store  <--  tools
      ^           ^
      |           |
    runtime ------+
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.knowledge.paths import DEFAULT_FOLDER, normalise_folder

logger = logging.getLogger(__name__)

REGISTRY_PATH = Path(__file__).with_name("registry.json")

# Ids owned by the static platform definition in `app.agents.runtime`. Allowing
# a dynamic agent or project to take one of these would shadow a built-in
# platform or project specialist at runtime.
PLATFORM_AGENT_IDS = frozenset(
    {
        "platform-builder",
        "platform-engineer",
        "platform-manager",
        "platform-telemetry",
        "platform-skills",
        "platform-scheduler",
        "platform-docs",
    }
)
PROJECT_TEMPLATE_IDS = frozenset(
    {
        "project-dev",
        "project-researcher",
        "dev-code-reviewer",
        "dev-test-engineer",
        "dev-security-auditor",
        "dev-perf-auditor",
    }
)
RESERVED_AGENT_IDS = PLATFORM_AGENT_IDS | PROJECT_TEMPLATE_IDS | frozenset({"oap"})
DEFAULT_PROJECT_MEMBERS = ("project-dev", "project-researcher")

_LOCK = threading.RLock()
_SUBSCRIBERS: list[Callable[[], None]] = []


class RegistryError(ValueError):
    """Raised for caller mistakes (reserved id, unknown agent, bad payload)."""


def _parse_str_list(val: Any) -> list[str]:
    if isinstance(val, list):
        return [str(s).strip() for s in val if str(s).strip()]
    if isinstance(val, str):
        return [s.strip() for s in val.split(",") if s.strip()]
    return []


@dataclass(slots=True)
class AgentSpec:
    agent_id: str
    name: str
    role: str = ""
    instructions: str = ""
    model_id: str | None = None
    knowledge_folder: str | None = None
    enable_web_search: bool = False
    skills: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AgentSpec:
        known = {f for f in cls.__dataclass_fields__ if f != "extra"}
        return cls(
            agent_id=str(raw["agent_id"]),
            name=str(raw.get("name") or raw["agent_id"]),
            role=str(raw.get("role") or ""),
            instructions=str(raw.get("instructions") or ""),
            model_id=raw.get("model_id") or None,
            knowledge_folder=raw.get("knowledge_folder") or None,
            enable_web_search=bool(raw.get("enable_web_search", False)),
            skills=_parse_str_list(raw.get("skills")),
            extra={k: v for k, v in raw.items() if k not in known},
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        extra = data.pop("extra", {}) or {}
        return {**extra, **data}


@dataclass(slots=True)
class ProjectSpec:
    project_id: str
    name: str
    description: str = ""
    instructions: str = ""
    members: list[str] = field(default_factory=lambda: list(DEFAULT_PROJECT_MEMBERS))
    knowledge_folder: str | None = None
    model_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ProjectSpec:
        known = {f for f in cls.__dataclass_fields__ if f != "extra"}
        pid = str(raw.get("project_id") or raw.get("team_id") or raw.get("id") or "").strip()
        members = _parse_str_list(raw.get("members"))
        if not members:
            members = list(DEFAULT_PROJECT_MEMBERS)
        return cls(
            project_id=pid,
            name=str(raw.get("name") or pid),
            description=str(raw.get("description") or raw.get("role") or ""),
            instructions=str(raw.get("instructions") or ""),
            members=members,
            knowledge_folder=raw.get("knowledge_folder") or None,
            model_id=raw.get("model_id") or None,
            extra={k: v for k, v in raw.items() if k not in known and k not in ("team_id", "id")},
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        extra = data.pop("extra", {}) or {}
        return {**extra, **data}


def subscribe(callback: Callable[[], None]) -> None:
    """Register a listener invoked after every successful registry mutation."""
    with _LOCK:
        if callback not in _SUBSCRIBERS:
            _SUBSCRIBERS.append(callback)


def _notify() -> None:
    for callback in list(_SUBSCRIBERS):
        callback()


def _registry_path() -> Path:
    return Path(os.environ.get("AGENT_REGISTRY_PATH", REGISTRY_PATH))


def _read_raw_registry() -> dict[str, Any]:
    path = _registry_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _write_raw_registry(payload: dict[str, Any]) -> None:
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def load_specs() -> list[AgentSpec]:
    with _LOCK:
        raw = _read_raw_registry()
    entries = raw.get("custom_agents") or []
    specs: list[AgentSpec] = []
    for entry in entries:
        if isinstance(entry, dict) and entry.get("agent_id"):
            specs.append(AgentSpec.from_dict(entry))
    return specs


def save_specs(specs: Iterable[AgentSpec]) -> None:
    """Atomically replace custom_agents in the registry file while preserving projects."""
    with _LOCK:
        raw = _read_raw_registry()
        raw["custom_agents"] = [spec.to_dict() for spec in specs]
        _write_raw_registry(raw)


def get_spec(agent_id: str) -> AgentSpec | None:
    for spec in load_specs():
        if spec.agent_id == agent_id:
            return spec
    return None


def upsert_spec(spec: AgentSpec) -> AgentSpec:
    """Create or replace a custom agent, preserving an existing folder binding."""
    if spec.agent_id in RESERVED_AGENT_IDS:
        raise RegistryError(f"'{spec.agent_id}' is a core platform agent and cannot be overwritten")
    if not spec.agent_id.strip():
        raise RegistryError("agent_id is required")
    if get_project(spec.agent_id) is not None:
        raise RegistryError(f"'{spec.agent_id}' is already a project ID")
    if spec.knowledge_folder:
        spec.knowledge_folder = normalise_folder(spec.knowledge_folder)
    if spec.skills:
        spec.skills = [str(s).strip() for s in spec.skills if str(s).strip()]

    specs = load_specs()
    for index, existing in enumerate(specs):
        if existing.agent_id == spec.agent_id:
            # A rebuild that omits the folder must not silently unbind the
            # agent's knowledge base.
            if not spec.knowledge_folder:
                spec.knowledge_folder = existing.knowledge_folder
            if not spec.skills and existing.skills:
                spec.skills = list(existing.skills)
            spec.extra = {**existing.extra, **spec.extra}
            specs[index] = spec
            break
    else:
        specs.append(spec)

    save_specs(specs)
    _notify()
    return spec


def sync_catalog_agents(specs: Iterable[AgentSpec], *, notify: bool = True) -> bool:
    """Replace catalog-sourced agents with `specs`.

    Agents created from the builder stay in the registry. Catalog files are the
    source of truth for agents marked ``source=catalog``. Returns True when the
    file changed. `notify` is False during startup, before the runtime exists.
    """
    project_ids = {project.project_id for project in load_projects()}
    incoming: dict[str, AgentSpec] = {}
    for spec in specs:
        if spec.agent_id in RESERVED_AGENT_IDS or not spec.agent_id.strip():
            raise RegistryError(f"'{spec.agent_id}' cannot be loaded from the catalog")
        if spec.agent_id in project_ids:
            logger.warning("Skipping catalog agent %s: its id is taken by a project", spec.agent_id)
            continue
        if not spec.knowledge_folder:
            spec.knowledge_folder = normalise_folder(f"agents/{spec.agent_id}")
        else:
            spec.knowledge_folder = normalise_folder(spec.knowledge_folder)
        spec.extra = {**spec.extra, "source": "catalog"}
        incoming[spec.agent_id] = spec

    current = load_specs()
    kept: list[AgentSpec] = []
    changed = False
    seen: set[str] = set()
    for existing in current:
        if existing.extra.get("source") != "catalog":
            kept.append(existing)
            continue
        replacement = incoming.get(existing.agent_id)
        if replacement is None:
            changed = True
            continue
        if replacement.to_dict() != existing.to_dict():
            changed = True
        kept.append(replacement)
        seen.add(existing.agent_id)
    for agent_id, spec in incoming.items():
        if agent_id not in seen:
            kept.append(spec)
            changed = True
    if changed:
        save_specs(kept)
        if notify:
            _notify()
    return changed


def delete_spec(agent_id: str) -> bool:
    specs = load_specs()
    remaining = [spec for spec in specs if spec.agent_id != agent_id]
    if len(remaining) == len(specs):
        return False
    save_specs(remaining)
    _notify()
    return True


def load_projects() -> list[ProjectSpec]:
    with _LOCK:
        raw = _read_raw_registry()
    entries = raw.get("projects") or []
    projects: list[ProjectSpec] = []
    for entry in entries:
        if isinstance(entry, dict) and (
            entry.get("project_id") or entry.get("team_id") or entry.get("id")
        ):
            projects.append(ProjectSpec.from_dict(entry))
    return projects


def save_projects(projects: Iterable[ProjectSpec]) -> None:
    """Atomically replace projects in the registry file while preserving custom_agents."""
    with _LOCK:
        raw = _read_raw_registry()
        raw.setdefault("custom_agents", [])
        raw["projects"] = [project.to_dict() for project in projects]
        _write_raw_registry(raw)


def get_project(project_id: str) -> ProjectSpec | None:
    for project in load_projects():
        if project.project_id == project_id:
            return project
    return None


def upsert_project(project: ProjectSpec) -> ProjectSpec:
    """Create or replace a user project, preserving an existing folder binding."""
    project.project_id = project.project_id.strip()
    if project.project_id in RESERVED_AGENT_IDS:
        raise RegistryError(
            f"'{project.project_id}' is a core platform or project template ID "
            "and cannot be overwritten"
        )
    if not project.project_id:
        raise RegistryError("project_id is required")
    if get_spec(project.project_id) is not None:
        raise RegistryError(f"'{project.project_id}' is already a custom agent ID")
    if project.knowledge_folder:
        project.knowledge_folder = normalise_folder(project.knowledge_folder)
    project.members = _parse_str_list(project.members) or list(DEFAULT_PROJECT_MEMBERS)

    projects = load_projects()
    for index, existing in enumerate(projects):
        if existing.project_id == project.project_id:
            if not project.knowledge_folder:
                project.knowledge_folder = existing.knowledge_folder or normalise_folder(
                    f"projects/{project.project_id}"
                )
            project.extra = {**existing.extra, **project.extra}
            projects[index] = project
            break
    else:
        if not project.knowledge_folder:
            project.knowledge_folder = normalise_folder(f"projects/{project.project_id}")
        projects.append(project)

    save_projects(projects)
    _notify()
    return project


def sync_catalog_projects(projects: Iterable[ProjectSpec], *, notify: bool = True) -> bool:
    """Replace catalog-sourced projects with `projects`."""
    agent_ids = {spec.agent_id for spec in load_specs()}
    incoming: dict[str, ProjectSpec] = {}
    for proj in projects:
        if proj.project_id in RESERVED_AGENT_IDS or not proj.project_id.strip():
            raise RegistryError(f"'{proj.project_id}' cannot be loaded from the catalog")
        if proj.project_id in agent_ids:
            logger.warning(
                "Skipping catalog project %s: its id is taken by a custom agent", proj.project_id
            )
            continue
        if not proj.knowledge_folder:
            proj.knowledge_folder = normalise_folder(f"projects/{proj.project_id}")
        else:
            proj.knowledge_folder = normalise_folder(proj.knowledge_folder)
        proj.members = _parse_str_list(proj.members) or list(DEFAULT_PROJECT_MEMBERS)
        proj.extra = {**proj.extra, "source": "catalog"}
        incoming[proj.project_id] = proj

    current = load_projects()
    kept: list[ProjectSpec] = []
    changed = False
    seen: set[str] = set()
    for existing in current:
        if existing.extra.get("source") != "catalog":
            kept.append(existing)
            continue
        replacement = incoming.get(existing.project_id)
        if replacement is None:
            changed = True
            continue
        if replacement.to_dict() != existing.to_dict():
            changed = True
        kept.append(replacement)
        seen.add(existing.project_id)
    for project_id, proj in incoming.items():
        if project_id not in seen:
            kept.append(proj)
            changed = True
    if changed:
        save_projects(kept)
        if notify:
            _notify()
    return changed


def delete_project_spec(project_id: str) -> bool:
    projects = load_projects()
    remaining = [p for p in projects if p.project_id != project_id]
    if len(remaining) == len(projects):
        return False
    save_projects(remaining)
    _notify()
    return True


def resolve_agent_target_folder(agent_id: str | None, explicit_folder: str | None = None) -> str:
    """Decide where a file belongs.

    Precedence: an explicit folder, `research` for `project-researcher`,
    `general` for reserved core/template agents, the project's bound knowledge
    folder for user projects, the agent's bound knowledge folder for custom
    agents, then `agents/<agent_id>`.
    """
    if explicit_folder and explicit_folder.strip():
        return normalise_folder(explicit_folder)
    if not agent_id:
        return DEFAULT_FOLDER
    if agent_id in ("project-researcher",):
        return normalise_folder("research")
    if agent_id in RESERVED_AGENT_IDS:
        return DEFAULT_FOLDER
    project = get_project(agent_id)
    if project is not None:
        return normalise_folder(project.knowledge_folder or f"projects/{agent_id}")
    spec = get_spec(agent_id)
    if spec and spec.knowledge_folder:
        return normalise_folder(spec.knowledge_folder)
    return normalise_folder(f"agents/{agent_id}")


def bind_folder_to_agent_if_custom(agent_id: str | None, folder: str) -> bool:
    """Bind `folder` to a custom agent that has no folder yet.

    Returns True when the registry changed. Core agents and already-bound
    agents are left alone, so a one-off upload cannot repoint an agent's
    knowledge base.
    """
    if not agent_id or agent_id in RESERVED_AGENT_IDS:
        return False
    spec = get_spec(agent_id)
    if spec is None or spec.knowledge_folder:
        return False
    spec.knowledge_folder = normalise_folder(folder)
    specs = [spec if s.agent_id == agent_id else s for s in load_specs()]
    save_specs(specs)
    _notify()
    return True
