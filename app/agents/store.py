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
import os
import tempfile
import threading
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.knowledge.paths import DEFAULT_FOLDER, normalise_folder

REGISTRY_PATH = Path(__file__).with_name("registry.json")

# Ids owned by the static platform definition in `app.agents.runtime`. Allowing
# a dynamic agent to take one of these would shadow a core agent at runtime.
RESERVED_AGENT_IDS = frozenset(
    {"platform-builder", "platform-engineer", "platform-manager", "agno"}
)

_LOCK = threading.RLock()
_SUBSCRIBERS: list[Callable[[], None]] = []


class RegistryError(ValueError):
    """Raised for caller mistakes (reserved id, unknown agent, bad payload)."""


@dataclass(slots=True)
class AgentSpec:
    agent_id: str
    name: str
    role: str = ""
    instructions: str = ""
    model_id: str | None = None
    knowledge_folder: str | None = None
    enable_web_search: bool = False
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
            extra={k: v for k, v in raw.items() if k not in known},
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


def load_specs() -> list[AgentSpec]:
    path = _registry_path()
    with _LOCK:
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            return []
    entries = raw.get("custom_agents") or []
    specs: list[AgentSpec] = []
    for entry in entries:
        if isinstance(entry, dict) and entry.get("agent_id"):
            specs.append(AgentSpec.from_dict(entry))
    return specs


def save_specs(specs: Iterable[AgentSpec]) -> None:
    """Atomically replace the registry file."""
    path = _registry_path()
    payload = {"custom_agents": [spec.to_dict() for spec in specs]}
    with _LOCK:
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
    if spec.knowledge_folder:
        spec.knowledge_folder = normalise_folder(spec.knowledge_folder)

    specs = load_specs()
    for index, existing in enumerate(specs):
        if existing.agent_id == spec.agent_id:
            # A rebuild that omits the folder must not silently unbind the
            # agent's knowledge base.
            if not spec.knowledge_folder:
                spec.knowledge_folder = existing.knowledge_folder
            spec.extra = {**existing.extra, **spec.extra}
            specs[index] = spec
            break
    else:
        specs.append(spec)

    save_specs(specs)
    _notify()
    return spec


def delete_spec(agent_id: str) -> bool:
    specs = load_specs()
    remaining = [spec for spec in specs if spec.agent_id != agent_id]
    if len(remaining) == len(specs):
        return False
    save_specs(remaining)
    _notify()
    return True


def resolve_agent_target_folder(agent_id: str | None, explicit_folder: str | None = None) -> str:
    """Decide where a file belongs.

    Precedence: an explicit folder, then the agent's bound knowledge folder,
    then `general` for core agents, then a per-agent folder for custom agents.
    """
    if explicit_folder and explicit_folder.strip():
        return normalise_folder(explicit_folder)
    if not agent_id or agent_id in RESERVED_AGENT_IDS:
        return DEFAULT_FOLDER
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
