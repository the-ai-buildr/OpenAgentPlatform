"""Tools for listing, inspecting, recommending, authoring, and syncing agent skills.

Used primarily by the `platform-skills` agent to manage OAP's bundled and
workspace-level skills (`app/skills/` and `workspace_files/skills/`).
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml
from agno.skills import validate_skill_directory
from agno.tools import tool

from app import skills_manager as sm
from app.settings import WORKSPACE_ROOT

logger = logging.getLogger(__name__)

BUNDLED_SKILLS_ROOT = sm.BUNDLED_SKILLS_ROOT
WORKSPACE_SKILLS_ROOT = WORKSPACE_ROOT / "skills"
_DEFAULT_WORKSPACE_SKILLS_ROOT = WORKSPACE_SKILLS_ROOT


def _sync_roots() -> Path:
    """Propagate any test monkeypatch of WORKSPACE_SKILLS_ROOT to skills_manager."""
    if WORKSPACE_SKILLS_ROOT != _DEFAULT_WORKSPACE_SKILLS_ROOT:
        sm.WORKSPACE_SKILLS_ROOT = Path(WORKSPACE_SKILLS_ROOT)
    return sm.ensure_workspace_skills_dir()


def list_platform_skills() -> dict[str, Any]:
    """List all available bundled and workspace skills with their metadata."""
    ws_dir = _sync_roots()
    skills = sm.list_all_skills()
    return {
        "status": "ok",
        "count": len(skills),
        "bundled_root": str(sm.BUNDLED_SKILLS_ROOT),
        "workspace_root": str(ws_dir),
        "skills": skills,
    }


def inspect_skill_details(skill_name: str) -> dict[str, Any]:
    """Return metadata, full instructions, references, and validation status for a skill."""
    _sync_roots()
    detail = sm.get_skill_detail(skill_name)
    if detail is None:
        return {
            "status": "not_found",
            "error": f"Skill {skill_name!r} not found",
            "skill_name": skill_name,
        }
    return {
        "status": "ok",
        "skill": detail,
        **detail,
    }


def recommend_skills_for_task(task_description: str, top_k: int = 5) -> dict[str, Any]:
    """Recommend relevant skills for a task description or specialist agent role."""
    _sync_roots()
    recommendations = sm.recommend_skills(task_description, top_k=top_k)
    return {
        "status": "ok",
        "task_description": task_description,
        "count": len(recommendations),
        "recommendations": recommendations,
        "recommended_skill_names": [item["name"] for item in recommendations],
    }


def sync_addy_agent_skills(
    repo_url: str = "https://github.com/addyosmani/agent-skills.git",
    branch: str = "main",
) -> dict[str, Any]:
    """Clone or pull `addyosmani/agent-skills` into workspace skills and validate them."""
    ws_root = _sync_roots()
    upstream_dir = ws_root / ".upstream_agent_skills"

    try:
        if (upstream_dir / ".git").is_dir():
            subprocess.run(
                ["git", "-C", str(upstream_dir), "fetch", "--depth", "1", "origin", branch],
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
            subprocess.run(
                ["git", "-C", str(upstream_dir), "reset", "--hard", "FETCH_HEAD"],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
        else:
            if upstream_dir.exists():
                shutil.rmtree(upstream_dir)
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    "--branch",
                    branch,
                    repo_url,
                    str(upstream_dir),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
    except (subprocess.SubprocessError, OSError) as exc:
        return {
            "status": "error",
            "error": f"Failed to sync repository {repo_url!r}: {exc}",
            "repo_url": repo_url,
            "branch": branch,
        }

    skills_dir = upstream_dir / "skills"
    skills_src_root = skills_dir if skills_dir.is_dir() else upstream_dir
    shared_refs_dir = upstream_dir / "references"
    shared_refs: dict[str, Path] = {}
    if shared_refs_dir.is_dir():
        for ref_path in sorted(shared_refs_dir.glob("*.md")):
            shared_refs[ref_path.name] = ref_path

    synced_skills: list[str] = []
    invalid_skills: dict[str, list[str]] = {}

    for candidate in sorted(skills_src_root.iterdir()):
        if not candidate.is_dir():
            continue
        if candidate.name.startswith(".") or candidate.name.startswith("_"):
            continue
        if not (candidate / "SKILL.md").is_file():
            continue

        errors = validate_skill_directory(candidate)
        if errors:
            invalid_skills[candidate.name] = errors
            continue

        dest = ws_root / candidate.name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(candidate, dest)

        # Copy any shared references mentioned in SKILL.md into the skill's references/
        skill_md_text = (dest / "SKILL.md").read_text(encoding="utf-8", errors="replace")
        for ref_name, ref_src in shared_refs.items():
            if ref_name in skill_md_text:
                refs_dst = dest / "references"
                refs_dst.mkdir(parents=True, exist_ok=True)
                target_ref = refs_dst / ref_name
                if not target_ref.exists():
                    shutil.copy2(ref_src, target_ref)

        synced_skills.append(candidate.name)

    return {
        "status": "ok",
        "repo_url": repo_url,
        "branch": branch,
        "synced_count": len(synced_skills),
        "skills": synced_skills,
        "invalid_skills": invalid_skills,
        "workspace_skills_root": str(ws_root),
    }


def create_or_update_custom_skill(
    skill_name: str,
    description: str,
    instructions: str,
    references: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Create or update a custom skill under `workspace_files/skills/<skill_name>/`."""
    raw_name = (skill_name or "").strip()
    if not raw_name or "/" in raw_name or "\\" in raw_name or ".." in raw_name:
        return {
            "status": "error",
            "error": "Invalid skill_name: must be a non-empty name without path separators",
        }

    normalized = re.sub(r"[^a-z0-9]+", "-", raw_name.lower()).strip("-")
    normalized = re.sub(r"-{2,}", "-", normalized)
    if not normalized or len(normalized) > 64:
        return {
            "status": "error",
            "error": (
                "Invalid skill_name: must normalize to 1-64 lowercase "
                "alphanumeric characters and hyphens"
            ),
        }

    clean_desc = (description or "").strip()
    if not clean_desc:
        return {
            "status": "error",
            "error": "Field 'description' must be a non-empty string",
        }
    if len(clean_desc) > 1024:
        return {
            "status": "error",
            "error": f"Description exceeds 1024 character limit ({len(clean_desc)} chars)",
        }

    clean_instructions = (instructions or "").strip()
    ws_root = _sync_roots()
    skill_dir = ws_root / normalized
    existed_before = skill_dir.exists()
    skill_dir.mkdir(parents=True, exist_ok=True)

    frontmatter = yaml.safe_dump(
        {"name": normalized, "description": clean_desc},
        sort_keys=False,
        allow_unicode=True,
    ).strip()
    skill_md_content = f"---\n{frontmatter}\n---\n\n{clean_instructions}\n"
    (skill_dir / "SKILL.md").write_text(skill_md_content, encoding="utf-8")

    written_refs: list[str] = []
    if references:
        refs_dir = skill_dir / "references"
        refs_dir.mkdir(parents=True, exist_ok=True)
        for ref_filename, ref_body in sorted(references.items()):
            safe_name = Path(ref_filename).name
            if not safe_name or safe_name.startswith("."):
                continue
            (refs_dir / safe_name).write_text(str(ref_body), encoding="utf-8")
            written_refs.append(safe_name)

    errors = validate_skill_directory(skill_dir)
    if errors:
        if not existed_before:
            shutil.rmtree(skill_dir, ignore_errors=True)
        return {
            "status": "error",
            "error": "; ".join(errors),
            "validation_errors": errors,
            "skill_name": normalized,
        }

    return {
        "status": "ok",
        "skill_name": normalized,
        "name": normalized,
        "description": clean_desc,
        "path": str(skill_dir),
        "references": written_refs,
        "valid": True,
    }


def delete_custom_skill(skill_name: str) -> dict[str, Any]:
    """Delete a custom workspace skill. Bundled platform skills cannot be deleted."""
    target = (skill_name or "").strip()
    if not target or "/" in target or "\\" in target or ".." in target:
        return {
            "status": "error",
            "error": "Invalid skill_name",
            "skill_name": target,
        }

    ws_root = _sync_roots()
    custom_dir = ws_root / target
    bundled_dir = sm.BUNDLED_SKILLS_ROOT / target

    if custom_dir.is_dir():
        shutil.rmtree(custom_dir)
        return {
            "status": "ok",
            "skill_name": target,
            "deleted_from": str(custom_dir),
        }

    if bundled_dir.is_dir():
        return {
            "status": "error",
            "error": f"'{target}' is a bundled platform skill and cannot be deleted",
            "skill_name": target,
        }

    return {
        "status": "not_found",
        "error": f"Custom skill '{target}' not found in workspace",
        "skill_name": target,
    }


delete_custom_skill.requires_confirmation = True  # type: ignore[attr-defined]

SKILL_TOOLS = [
    list_platform_skills,
    inspect_skill_details,
    recommend_skills_for_task,
    sync_addy_agent_skills,
    create_or_update_custom_skill,
    tool(requires_confirmation=True)(delete_custom_skill),
]

__all__ = [
    "BUNDLED_SKILLS_ROOT",
    "SKILL_TOOLS",
    "WORKSPACE_SKILLS_ROOT",
    "create_or_update_custom_skill",
    "delete_custom_skill",
    "inspect_skill_details",
    "list_platform_skills",
    "recommend_skills_for_task",
    "sync_addy_agent_skills",
]
