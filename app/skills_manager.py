"""Skill loading, discovery, validation, and recommendation for OAP agents.

Manages two skill roots:
- `BUNDLED_SKILLS_ROOT` (`app/skills/`): seeded with the 25 skills from
  `addyosmani/agent-skills` plus OAP's custom `human-in-the-loop` skill.
- `WORKSPACE_SKILLS_ROOT` (`workspace_files/skills/`): runtime-writable
  directory for git-synced updates and custom agent/operator-authored skills.
  Workspace skills load after bundled skills so they can override bundled
  definitions by name.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from agno.skills import LocalSkills, Skill, Skills, validate_skill_directory
from agno.skills.errors import SkillValidationError

import app.settings as _settings_module
from app.settings import WORKSPACE_ROOT

logger = logging.getLogger(__name__)

BUNDLED_SKILLS_ROOT = Path(__file__).resolve().parent / "skills"
WORKSPACE_SKILLS_ROOT = WORKSPACE_ROOT / "skills"

_DEFAULT_WORKSPACE_ROOT = WORKSPACE_ROOT
_DEFAULT_WORKSPACE_SKILLS_ROOT = WORKSPACE_SKILLS_ROOT

_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "how",
        "in",
        "into",
        "is",
        "it",
        "its",
        "need",
        "needs",
        "of",
        "on",
        "or",
        "our",
        "should",
        "that",
        "the",
        "their",
        "them",
        "this",
        "to",
        "use",
        "using",
        "want",
        "we",
        "what",
        "when",
        "where",
        "which",
        "who",
        "will",
        "with",
    }
)

# Lifecycle and domain keyword hints to improve skill recommendation accuracy.
_LIFECYCLE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "api-and-interface-design": (
        "api",
        "rest",
        "graphql",
        "endpoint",
        "endpoints",
        "contract",
        "schema",
        "interface",
        "sdk",
        "versioning",
    ),
    "browser-testing-with-devtools": (
        "browser",
        "devtools",
        "playwright",
        "selenium",
        "dom",
        "console",
        "e2e",
        "screenshot",
        "network",
    ),
    "ci-cd-and-automation": (
        "ci",
        "cd",
        "pipeline",
        "github",
        "actions",
        "workflow",
        "automation",
        "build",
        "lint",
        "deploy",
    ),
    "code-review-and-quality": (
        "review",
        "quality",
        "pr",
        "pull",
        "maintainability",
        "readability",
        "audit",
        "diff",
        "bug",
    ),
    "code-simplification": (
        "simplify",
        "simplification",
        "refactor",
        "refactoring",
        "clean",
        "complexity",
        "dead",
        "duplication",
    ),
    "constraint-driven-development": (
        "constraint",
        "constraints",
        "guardrail",
        "guardrails",
        "boundary",
        "limit",
        "floor",
    ),
    "context-engineering": (
        "context",
        "prompt",
        "memory",
        "token",
        "tokens",
        "retrieval",
        "rag",
        "grounding",
    ),
    "debugging-and-error-recovery": (
        "debug",
        "debugging",
        "error",
        "exception",
        "traceback",
        "crash",
        "fix",
        "root",
        "cause",
        "recovery",
    ),
    "deprecation-and-migration": (
        "deprecate",
        "deprecation",
        "migrate",
        "migration",
        "upgrade",
        "legacy",
        "codemod",
        "breaking",
    ),
    "documentation-and-adrs": (
        "doc",
        "docs",
        "documentation",
        "adr",
        "readme",
        "architecture",
        "decision",
        "runbook",
    ),
    "doubt-driven-development": (
        "doubt",
        "verify",
        "verification",
        "skeptic",
        "hallucination",
        "assumption",
        "validate",
    ),
    "frontend-ui-engineering": (
        "frontend",
        "ui",
        "ux",
        "react",
        "css",
        "tailwind",
        "component",
        "accessibility",
        "a11y",
        "responsive",
    ),
    "git-workflow-and-versioning": (
        "git",
        "branch",
        "commit",
        "merge",
        "rebase",
        "semver",
        "release",
        "changelog",
    ),
    "human-in-the-loop": (
        "hitl",
        "human",
        "approval",
        "approve",
        "confirm",
        "confirmation",
        "reject",
        "pause",
        "paused",
        "resume",
        "continue",
        "requires_confirmation",
        "destructive",
    ),
    "idea-refine": (
        "idea",
        "brainstorm",
        "refine",
        "ideation",
        "product",
        "concept",
        "explore",
    ),
    "incremental-implementation": (
        "incremental",
        "slice",
        "step",
        "iterative",
        "small",
        "atomic",
        "implementation",
    ),
    "interview-me": (
        "interview",
        "questions",
        "clarify",
        "requirements",
        "discovery",
        "stakeholder",
    ),
    "observability-and-instrumentation": (
        "observability",
        "telemetry",
        "otel",
        "opentelemetry",
        "trace",
        "tracing",
        "span",
        "metrics",
        "logging",
        "logs",
        "phoenix",
    ),
    "performance-optimization": (
        "performance",
        "perf",
        "latency",
        "throughput",
        "optimize",
        "optimization",
        "memory",
        "profiling",
        "bottleneck",
        "cache",
        "vitals",
    ),
    "planning-and-task-breakdown": (
        "plan",
        "planning",
        "breakdown",
        "task",
        "tasks",
        "milestone",
        "roadmap",
        "sequence",
    ),
    "security-and-hardening": (
        "security",
        "secure",
        "owasp",
        "vulnerability",
        "vulnerabilities",
        "auth",
        "authentication",
        "authorization",
        "secret",
        "secrets",
        "xss",
        "csrf",
        "injection",
        "hardening",
    ),
    "shipping-and-launch": (
        "ship",
        "shipping",
        "launch",
        "production",
        "rollout",
        "checklist",
        "readiness",
    ),
    "source-driven-development": (
        "source",
        "sources",
        "citation",
        "citations",
        "research",
        "evidence",
        "paper",
        "arxiv",
        "reference",
    ),
    "spec-driven-development": (
        "spec",
        "specification",
        "design",
        "requirements",
        "prd",
        "blueprint",
    ),
    "test-driven-development": (
        "test",
        "tests",
        "testing",
        "tdd",
        "pytest",
        "unit",
        "integration",
        "coverage",
        "assertion",
        "regression",
    ),
    "using-agent-skills": (
        "skill",
        "skills",
        "workflow",
        "orchestration",
        "lifecycle",
        "meta",
    ),
}


def _resolve_workspace_skills_root() -> Path:
    """Resolve the active workspace skills directory, honoring test monkeypatches."""
    if WORKSPACE_SKILLS_ROOT != _DEFAULT_WORKSPACE_SKILLS_ROOT:
        return Path(WORKSPACE_SKILLS_ROOT)
    if WORKSPACE_ROOT != _DEFAULT_WORKSPACE_ROOT:
        return Path(WORKSPACE_ROOT) / "skills"
    return Path(_settings_module.WORKSPACE_ROOT) / "skills"


def ensure_workspace_skills_dir() -> Path:
    """Ensure the runtime-writable workspace skills directory exists and return it."""
    ws_dir = _resolve_workspace_skills_root()
    ws_dir.mkdir(parents=True, exist_ok=True)
    return ws_dir


class _SafeLocalSkills(LocalSkills):
    """LocalSkills subclass that skips '.' and '_' helper folders and filters by name."""

    def __init__(
        self,
        path: str | Path,
        validate: bool = True,
        allowed_names: set[str] | None = None,
    ) -> None:
        super().__init__(path=str(path), validate=validate)
        self.allowed_names = allowed_names

    def load(self) -> list[Skill]:
        if not self.path.exists():
            return []

        skills: list[Skill] = []
        skill_md_path = self.path / "SKILL.md"
        if skill_md_path.exists():
            skill = self._load_skill_from_folder(self.path)
            if skill and (self.allowed_names is None or skill.name in self.allowed_names):
                skills.append(skill)
            return skills

        for item in sorted(self.path.iterdir()):
            if not item.is_dir():
                continue
            if item.name.startswith(".") or item.name.startswith("_"):
                continue
            if not (item / "SKILL.md").exists():
                continue
            if self.allowed_names is not None and item.name not in self.allowed_names:
                # Fast-path skip when directory name does not match requested skills
                # (validated skills always have name == directory name).
                continue
            try:
                skill = self._load_skill_from_folder(item)
            except SkillValidationError as exc:
                # One hand-edited workspace skill must not take down every agent
                # that loads skills.
                logger.warning("Skipping invalid skill %s: %s", item, exc)
                continue
            if skill and (self.allowed_names is None or skill.name in self.allowed_names):
                skills.append(skill)

        return skills


def _load_bundled_and_workspace_maps() -> tuple[dict[str, Skill], dict[str, Skill]]:
    """Return `(bundled_skills_by_name, workspace_skills_by_name)`."""
    bundled_map: dict[str, Skill] = {}
    if BUNDLED_SKILLS_ROOT.is_dir():
        for skill in _SafeLocalSkills(BUNDLED_SKILLS_ROOT, validate=True).load():
            bundled_map[skill.name] = skill

    workspace_map: dict[str, Skill] = {}
    ws_root = _resolve_workspace_skills_root()
    if ws_root.is_dir():
        for skill in _SafeLocalSkills(ws_root, validate=True).load():
            workspace_map[skill.name] = skill

    return bundled_map, workspace_map


def build_skills(skill_names: list[str] | None = None) -> Skills | None:
    """Build an Agno `Skills` object from bundled and workspace skill directories.

    - `skill_names is None` or contains `"*"`: loads all available skills.
    - `skill_names == []`: returns `None`.
    - `skill_names == ["name-a", ...]`: loads only the matching skills, or
      returns `None` if no matching skills exist.
    """
    allowed_names: set[str] | None = None
    if skill_names is not None:
        if len(skill_names) == 0:
            return None
        cleaned = [str(name).strip() for name in skill_names if str(name).strip()]
        if not cleaned:
            return None
        if "*" not in cleaned and "all" not in {c.lower() for c in cleaned}:
            allowed_names = set(cleaned)

    loaders: list[_SafeLocalSkills] = []
    if BUNDLED_SKILLS_ROOT.is_dir():
        loaders.append(
            _SafeLocalSkills(
                BUNDLED_SKILLS_ROOT,
                validate=True,
                allowed_names=allowed_names,
            )
        )

    ws_root = _resolve_workspace_skills_root()
    if ws_root.is_dir():
        loaders.append(
            _SafeLocalSkills(
                ws_root,
                validate=True,
                allowed_names=allowed_names,
            )
        )

    if not loaders:
        return None

    skills_obj = Skills(loaders=loaders)
    if not skills_obj.get_all_skills():
        return None
    return skills_obj


def list_all_skills() -> list[dict[str, Any]]:
    """Return metadata dicts for all available skills (workspace overrides bundled)."""
    bundled_map, workspace_map = _load_bundled_and_workspace_maps()
    merged: dict[str, tuple[Skill, str, bool]] = {}

    for name, skill in bundled_map.items():
        merged[name] = (skill, "bundled", False)

    for name, skill in workspace_map.items():
        merged[name] = (skill, "workspace", name in bundled_map)

    results: list[dict[str, Any]] = []
    for name in sorted(merged):
        skill, source, overridden = merged[name]
        results.append(
            {
                "name": skill.name,
                "description": skill.description,
                "scripts": list(skill.scripts or []),
                "references": list(skill.references or []),
                "source": source,
                "source_path": skill.source_path,
                "overridden": overridden,
            }
        )
    return results


def get_skill_detail(skill_name: str) -> dict[str, Any] | None:
    """Return full details, instructions, references, and validation status for one skill."""
    target = (skill_name or "").strip()
    if not target:
        return None

    bundled_map, workspace_map = _load_bundled_and_workspace_maps()
    if target in workspace_map:
        skill = workspace_map[target]
        source = "workspace"
        overridden = target in bundled_map
    elif target in bundled_map:
        skill = bundled_map[target]
        source = "bundled"
        overridden = False
    else:
        return None

    validation_errors = validate_skill_directory(Path(skill.source_path))
    return {
        "name": skill.name,
        "description": skill.description,
        "instructions": skill.instructions,
        "scripts": list(skill.scripts or []),
        "references": list(skill.references or []),
        "source": source,
        "source_path": skill.source_path,
        "overridden": overridden,
        "metadata": skill.metadata,
        "license": skill.license,
        "compatibility": skill.compatibility,
        "allowed_tools": skill.allowed_tools,
        "valid": len(validation_errors) == 0,
        "validation_errors": validation_errors,
    }


def _tokenize(text: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9_]+", (text or "").lower())
    return [tok for tok in tokens if len(tok) > 1 and tok not in _STOP_WORDS]


def recommend_skills(task_description: str, top_k: int = 5) -> list[dict[str, Any]]:
    """Recommend the most relevant skills for a task description or agent role."""
    bundled_map, workspace_map = _load_bundled_and_workspace_maps()
    merged: dict[str, tuple[Skill, str]] = {
        name: (skill, "bundled") for name, skill in bundled_map.items()
    }
    for name, skill in workspace_map.items():
        merged[name] = (skill, "workspace")

    if not merged:
        return []

    limit = max(1, int(top_k or 5))
    query_lower = (task_description or "").strip().lower()
    query_tokens = _tokenize(query_lower)

    scored: list[tuple[float, str, dict[str, Any]]] = []
    for name, (skill, source) in merged.items():
        name_tokens = set(_tokenize(skill.name.replace("-", " ")))
        desc_tokens = set(_tokenize(skill.description))
        instr_tokens = set(_tokenize(skill.instructions))
        lifecycle_tokens = set(_LIFECYCLE_KEYWORDS.get(name, ()))

        score = 0.0
        matched_terms: set[str] = set()

        if skill.name in query_lower:
            score += 12.0
            matched_terms.add(skill.name)

        for tok in query_tokens:
            if tok in name_tokens or any(
                nt.startswith(tok) or tok.startswith(nt) for nt in name_tokens if len(nt) >= 4
            ):
                score += 5.0
                matched_terms.add(tok)
            if tok in lifecycle_tokens or any(
                lt.startswith(tok) or tok.startswith(lt) for lt in lifecycle_tokens if len(lt) >= 4
            ):
                score += 4.0
                matched_terms.add(tok)
            if tok in desc_tokens:
                score += 2.5
                matched_terms.add(tok)
            elif tok in instr_tokens:
                score += 1.0
                matched_terms.add(tok)

        if score > 0:
            scored.append(
                (
                    score,
                    name,
                    {
                        "name": skill.name,
                        "description": skill.description,
                        "source": source,
                        "score": round(score, 2),
                        "matched_terms": sorted(matched_terms),
                        "references": list(skill.references or []),
                    },
                )
            )

    if scored:
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [item[2] for item in scored[:limit]]

    # Fallback foundational skills when query has no specific keyword overlap
    defaults = [
        "using-agent-skills",
        "spec-driven-development",
        "planning-and-task-breakdown",
        "test-driven-development",
        "code-review-and-quality",
    ]
    fallback: list[dict[str, Any]] = []
    for name in defaults:
        if name in merged and len(fallback) < limit:
            skill, source = merged[name]
            fallback.append(
                {
                    "name": skill.name,
                    "description": skill.description,
                    "source": source,
                    "score": 1.0,
                    "matched_terms": [],
                    "references": list(skill.references or []),
                }
            )
    return fallback


__all__ = [
    "BUNDLED_SKILLS_ROOT",
    "WORKSPACE_SKILLS_ROOT",
    "build_skills",
    "ensure_workspace_skills_dir",
    "get_skill_detail",
    "list_all_skills",
    "recommend_skills",
]
