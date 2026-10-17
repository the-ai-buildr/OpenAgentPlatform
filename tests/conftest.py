import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The application reads configuration at import time; pin a deterministic,
# key-free environment so tests never depend on a developer's .env.
os.environ.setdefault("OTEL_ENABLED", "false")
os.environ.setdefault("ENABLE_FILE_WATCHER", "false")
os.environ.setdefault("OPENROUTER_API_KEY", "")
os.environ.setdefault("EMBEDDING_API_KEY", "")


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """Redirect the workspace root at every module that captured it."""
    root = tmp_path / "workspace_files"
    (root / "general").mkdir(parents=True)
    import app.settings as settings_module

    monkeypatch.setattr(settings_module, "WORKSPACE_ROOT", root)
    for module_name in ("app.knowledge.knowledge_manager", "app.tools.platform_tools"):
        module = __import__(module_name, fromlist=["WORKSPACE_ROOT"])
        if hasattr(module, "WORKSPACE_ROOT"):
            monkeypatch.setattr(module, "WORKSPACE_ROOT", root)
    return root


@pytest.fixture()
def registry_file(tmp_path, monkeypatch):
    """Point the agent registry at a throwaway file."""
    path = tmp_path / "registry.json"
    monkeypatch.setenv("AGENT_REGISTRY_PATH", str(path))
    from app.agents import store

    monkeypatch.setattr(store, "_SUBSCRIBERS", [])
    return path
