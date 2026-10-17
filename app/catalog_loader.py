"""Load custom agents, teams and workflows from `app/catalog`.

A file named `research.json` becomes the model `research` in Open WebUI.
Its default knowledge folder is `workspace_files/agents/research`, so files
dropped there belong to that agent. Files whose names start with `_` or `.`
are examples and are not registered.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agents.store import RESERVED_AGENT_IDS, AgentSpec, RegistryError, sync_catalog_agents
from app.knowledge.paths import folder_dir
from app.settings import WORKSPACE_ROOT

logger = logging.getLogger(__name__)

CATALOG_ROOT = Path(__file__).resolve().parent / "catalog"
AGENTS_DIR = CATALOG_ROOT / "agents"
TEAMS_DIR = CATALOG_ROOT / "teams"
WORKFLOWS_DIR = CATALOG_ROOT / "workflows"

_SLUG = re.compile(r"[^a-z0-9]+")
_team_files: list[TeamFile] = []
_workflow_files: list[WorkflowFile] = []
_watcher: Any = None


@dataclass(slots=True)
class TeamFile:
    team_id: str
    name: str
    instructions: str = ""
    members: list[str] = field(default_factory=list)


@dataclass(slots=True)
class WorkflowFile:
    workflow_id: str
    name: str
    description: str = ""
    agent_id: str = ""


def component_id(path: Path) -> str | None:
    """Id from a catalog filename. Examples and reserved ids are skipped."""
    stem = path.stem.strip().lower()
    if not stem or stem.startswith(("_", ".")):
        return None
    slug = _SLUG.sub("-", stem).strip("-")
    if not slug or slug in RESERVED_AGENT_IDS:
        logger.warning("Skipping catalog file %s: id %r is reserved or empty", path.name, slug)
        return None
    return slug


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Skipping catalog file %s: %s", path.name, exc)
        return None
    if not isinstance(raw, dict):
        logger.warning("Skipping catalog file %s: expected a JSON object", path.name)
        return None
    return raw


def _json_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.glob("*.json") if path.is_file())


def read_agent_files(directory: Path | None = None) -> list[AgentSpec]:
    specs: list[AgentSpec] = []
    for path in _json_files(directory or AGENTS_DIR):
        agent_id = component_id(path)
        raw = _read_json(path)
        if agent_id is None or raw is None:
            continue
        raw["agent_id"] = agent_id
        raw.setdefault("knowledge_folder", f"agents/{agent_id}")
        try:
            specs.append(AgentSpec.from_dict(raw))
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Skipping catalog agent %s: %s", path.name, exc)
    return specs


def read_team_files(directory: Path | None = None) -> list[TeamFile]:
    teams: list[TeamFile] = []
    for path in _json_files(directory or TEAMS_DIR):
        team_id = component_id(path)
        raw = _read_json(path)
        if team_id is None or raw is None:
            continue
        members = raw.get("members") or []
        if not isinstance(members, list):
            logger.warning("Skipping catalog team %s: members must be a list", path.name)
            continue
        teams.append(
            TeamFile(
                team_id=team_id,
                name=str(raw.get("name") or team_id),
                instructions=str(raw.get("instructions") or ""),
                members=[str(member) for member in members if str(member).strip()],
            )
        )
    return teams


def read_workflow_files(directory: Path | None = None) -> list[WorkflowFile]:
    workflows: list[WorkflowFile] = []
    for path in _json_files(directory or WORKFLOWS_DIR):
        workflow_id = component_id(path)
        raw = _read_json(path)
        if workflow_id is None or raw is None:
            continue
        agent_id = str(raw.get("agent_id") or "").strip()
        if not agent_id:
            logger.warning("Skipping catalog workflow %s: agent_id is required", path.name)
            continue
        workflows.append(
            WorkflowFile(
                workflow_id=workflow_id,
                name=str(raw.get("name") or workflow_id),
                description=str(raw.get("description") or ""),
                agent_id=agent_id,
            )
        )
    return workflows


def team_files() -> list[TeamFile]:
    return list(_team_files)


def workflow_files() -> list[WorkflowFile]:
    return list(_workflow_files)


def _ensure_folder(folder: str) -> None:
    try:
        folder_dir(WORKSPACE_ROOT, folder).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("Could not create workspace folder %s: %s", folder, exc)


def refresh(*, notify: bool = True) -> None:
    """Read the catalog and sync agents into the registry."""
    global _team_files, _workflow_files
    for directory in (AGENTS_DIR, TEAMS_DIR, WORKFLOWS_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    specs = read_agent_files()
    try:
        sync_catalog_agents(specs, notify=notify)
    except RegistryError as exc:
        logger.warning("Catalog agent sync failed: %s", exc)
    for spec in specs:
        if spec.knowledge_folder:
            _ensure_folder(spec.knowledge_folder)
    _team_files = read_team_files()
    _workflow_files = read_workflow_files()


def start_watcher(on_change: Any) -> None:
    """Reload the catalog when a definition file changes."""
    global _watcher
    if _watcher is not None:
        return
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError:  # pragma: no cover - watchdog is a runtime dependency
        logger.warning("watchdog is not installed; catalog files will load only at startup")
        return

    class Handler(FileSystemEventHandler):
        def on_any_event(self, event: Any) -> None:
            if getattr(event, "is_directory", False):
                return
            path = str(getattr(event, "dest_path", None) or event.src_path)
            if path.endswith(".json"):
                on_change()

    CATALOG_ROOT.mkdir(parents=True, exist_ok=True)
    observer = Observer()
    observer.schedule(Handler(), str(CATALOG_ROOT), recursive=True)
    observer.daemon = True
    observer.start()
    _watcher = observer
    logger.info("Catalog watcher started on %s", CATALOG_ROOT)


def stop_watcher() -> None:
    global _watcher
    if _watcher is not None:
        _watcher.stop()
        _watcher.join(timeout=2)
        _watcher = None
