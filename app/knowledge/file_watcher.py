"""Debounced filesystem watcher that keeps PgVector in sync with the workspace.

Editors write files in bursts (temp file, rename, truncate, write), so every
path gets its own debounce timer and only the last event in a burst triggers an
embedding run.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from app.knowledge.knowledge_manager import index_single_file
from app.knowledge.paths import SUPPORTED_EXTENSIONS, is_supported_file
from app.settings import WORKSPACE_ROOT, settings
from app.telemetry.otel import get_tracer

logger = logging.getLogger(__name__)
tracer = get_tracer("agno.watcher")

MAX_EVENT_LOG = 50
WATCHER_EVENT_LOG: deque[dict[str, Any]] = deque(maxlen=MAX_EVENT_LOG)


def _should_index(path: Path) -> bool:
    if any(part.startswith(".") for part in path.parts):
        return False
    if path.name.endswith("~") or path.name.startswith("~"):
        return False
    return is_supported_file(path)


class DebouncedIndexHandler(FileSystemEventHandler):
    def __init__(self, debounce_seconds: float) -> None:
        self.debounce_seconds = max(debounce_seconds, 0.1)
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def on_created(self, event: FileSystemEvent) -> None:
        self._schedule(event, "created")

    def on_modified(self, event: FileSystemEvent) -> None:
        self._schedule(event, "modified")

    def on_moved(self, event: FileSystemEvent) -> None:
        destination = getattr(event, "dest_path", None)
        if destination:
            self._schedule_path(Path(str(destination)), "moved")

    def _schedule(self, event: FileSystemEvent, kind: str) -> None:
        if event.is_directory:
            return
        self._schedule_path(Path(str(event.src_path)), kind)

    def _schedule_path(self, path: Path, kind: str) -> None:
        if not _should_index(path):
            return
        key = str(path)
        with self._lock:
            existing = self._timers.pop(key, None)
            if existing is not None:
                existing.cancel()
            timer = threading.Timer(self.debounce_seconds, self._run_index, args=(path, kind))
            timer.daemon = True
            self._timers[key] = timer
            timer.start()

    def _run_index(self, path: Path, kind: str) -> None:
        with self._lock:
            self._timers.pop(str(path), None)
        entry: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": kind,
            "path": str(path),
        }
        with tracer.start_as_current_span("watcher.auto_index_event") as span:
            span.set_attribute("file.path", str(path))
            span.set_attribute("watcher.event", kind)
            try:
                entry["result"] = index_single_file(path, recreate=True)
                entry["status"] = "ok"
            except Exception as exc:
                entry["status"] = "error"
                entry["error"] = str(exc)
                logger.warning("Auto-index failed for %s: %s", path, exc)
        WATCHER_EVENT_LOG.append(entry)

    def cancel_pending(self) -> None:
        with self._lock:
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()


class WorkspaceWatcherService:
    def __init__(self) -> None:
        self._observer: Observer | None = None  # type: ignore[valid-type]
        self._handler: DebouncedIndexHandler | None = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._observer is not None and self._observer.is_alive()

    def start(self) -> bool:
        with self._lock:
            if self.running:
                return True
            WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
            self._handler = DebouncedIndexHandler(settings.watcher_debounce_seconds)
            observer = Observer()
            observer.schedule(self._handler, str(WORKSPACE_ROOT), recursive=True)
            observer.daemon = True
            observer.start()
            self._observer = observer
            logger.info("Workspace watcher started on %s", WORKSPACE_ROOT)
            return True

    def stop(self) -> None:
        with self._lock:
            if self._handler is not None:
                self._handler.cancel_pending()
            observer = self._observer
            self._observer = None
            if observer is not None:
                observer.stop()
                observer.join(timeout=5)
                logger.info("Workspace watcher stopped")

    def status(self) -> dict[str, Any]:
        return {
            "watcher_enabled": settings.enable_file_watcher,
            "watcher_running": self.running,
            "watched_root": str(WORKSPACE_ROOT),
            "debounce_seconds": settings.watcher_debounce_seconds,
            "supported_extensions": sorted(SUPPORTED_EXTENSIONS),
            "recent_events": list(WATCHER_EVENT_LOG)[-20:],
        }


watcher_service = WorkspaceWatcherService()
