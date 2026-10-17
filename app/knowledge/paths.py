"""Pure path and naming helpers for the workspace knowledge tree.

This module deliberately has no Agno, database or network dependencies so the
security-critical logic (traversal containment, table naming) can be unit
tested without any infrastructure.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path, PurePosixPath

SUPPORTED_EXTENSIONS = frozenset({".pdf", ".txt", ".md", ".csv", ".json", ".docx"})

DEFAULT_FOLDER = "general"
TABLE_PREFIX = "kb_"
MAX_TABLE_NAME_LENGTH = 60
# 36 characters of UUID plus the underscore Open WebUI prefixes onto uploads.
_OPENWEBUI_UPLOAD_PREFIX = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}_")
_UNSAFE_SEGMENT_CHARS = re.compile(r"[^a-z0-9._-]+")
_SLUG_CHARS = re.compile(r"[^a-z0-9]+")


class UnsafePathError(ValueError):
    """Raised when a caller-supplied path would escape the workspace root."""


def normalise_folder(folder: str | None) -> str:
    """Return a relative POSIX folder key, or raise if it escapes the root.

    The returned value is always relative, lowercase-safe and free of `.` and
    `..` segments. It is *not* yet joined to the workspace root.
    """
    raw = (folder or "").strip()
    if not raw:
        return DEFAULT_FOLDER

    # Windows-style separators arrive from Open WebUI clients often enough to
    # be worth normalising rather than rejecting.
    raw = raw.replace("\\", "/")
    if raw.startswith("~"):
        raise UnsafePathError("Home-relative folders are not allowed")

    segments: list[str] = []
    for segment in PurePosixPath(raw).parts:
        if segment in ("/", ".", ""):
            continue
        if segment == "..":
            raise UnsafePathError(f"Folder path escapes the workspace root: {folder!r}")
        cleaned = _UNSAFE_SEGMENT_CHARS.sub("-", segment.lower()).strip("-.")
        if not cleaned:
            raise UnsafePathError(f"Folder path has an empty segment: {folder!r}")
        segments.append(cleaned)

    if not segments:
        return DEFAULT_FOLDER
    return "/".join(segments)


def resolve_within(root: Path, candidate: str | Path) -> Path:
    """Resolve `candidate` under `root`, rejecting anything that escapes it.

    Uses real path resolution plus `Path.is_relative_to`, which — unlike a
    string `startswith` check — is not fooled by sibling directories that share
    a name prefix (`/app/workspace_files_evil`) or by symlinks.
    """
    root_resolved = Path(root).resolve()
    target = Path(candidate)
    resolved = target.resolve() if target.is_absolute() else (root_resolved / target).resolve()
    if resolved != root_resolved and not resolved.is_relative_to(root_resolved):
        raise UnsafePathError(f"Path escapes {root_resolved}: {candidate!r}")
    return resolved


def folder_dir(root: Path, folder: str | None) -> Path:
    """Absolute directory for a workspace folder key."""
    return resolve_within(root, normalise_folder(folder))


def folder_to_table_name(folder: str | None) -> str:
    """Map a folder key onto a deterministic PgVector table name.

    Table names are truncated to keep the generated index names inside
    PostgreSQL's 63-byte identifier limit.
    """
    key = normalise_folder(folder)
    slug = _SLUG_CHARS.sub("_", unicodedata.normalize("NFKD", key).lower()).strip("_")
    if not slug:
        slug = DEFAULT_FOLDER
    return (TABLE_PREFIX + slug)[:MAX_TABLE_NAME_LENGTH].rstrip("_")


def is_supported_file(path: str | Path) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_EXTENSIONS


def strip_openwebui_prefix(filename: str) -> str:
    """Remove the `<uuid>_` prefix Open WebUI puts on stored uploads."""
    return _OPENWEBUI_UPLOAD_PREFIX.sub("", filename)


def safe_filename(filename: str) -> str:
    """Reduce an arbitrary upload name to a single safe path segment."""
    name = Path(filename.replace("\\", "/")).name
    name = _UNSAFE_SEGMENT_CHARS.sub("-", name.lower()).strip("-.")
    if not name:
        raise UnsafePathError(f"Filename reduces to nothing: {filename!r}")
    return name


def relative_folder_for(root: Path, file_path: Path) -> str:
    """Folder key that owns `file_path`, defaulting to `general` at the root."""
    resolved = resolve_within(root, file_path)
    parent = resolved.parent
    root_resolved = Path(root).resolve()
    if parent == root_resolved:
        return DEFAULT_FOLDER
    return parent.relative_to(root_resolved).as_posix()
