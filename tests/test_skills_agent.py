"""Skill loading, validation, authoring, recommendation and agent binding.

Bundled skills are read from the repository; workspace skills are redirected
to `tmp_path`. Git sync is exercised against a local fake upstream, so these
tests need no network.
"""

from __future__ import annotations

import inspect
import subprocess
from pathlib import Path

import pytest
from agno.skills import Skills, validate_skill_directory
from agno.tools.function import Function

from app import skills_manager as sm
from app.tools import skill_tools as st

ADDY_SKILLS = frozenset(
    {
        "api-and-interface-design",
        "browser-testing-with-devtools",
        "ci-cd-and-automation",
        "code-review-and-quality",
        "code-simplification",
        "constraint-driven-development",
        "context-engineering",
        "debugging-and-error-recovery",
        "deprecation-and-migration",
        "documentation-and-adrs",
        "doubt-driven-development",
        "frontend-ui-engineering",
        "git-workflow-and-versioning",
        "idea-refine",
        "incremental-implementation",
        "interview-me",
        "observability-and-instrumentation",
        "performance-optimization",
        "planning-and-task-breakdown",
        "security-and-hardening",
        "shipping-and-launch",
        "source-driven-development",
        "spec-driven-development",
        "test-driven-development",
        "using-agent-skills",
    }
)
BUNDLED_SKILLS = ADDY_SKILLS | {"human-in-the-loop"}


@pytest.fixture()
def skills_ws(tmp_path, monkeypatch):
    """Redirect the writable workspace skills root into `tmp_path`."""
    root = tmp_path / "workspace_files" / "skills"
    monkeypatch.setattr(sm, "WORKSPACE_SKILLS_ROOT", root)
    monkeypatch.setattr(st, "WORKSPACE_SKILLS_ROOT", root)
    return root


def write_skill(root: Path, name: str, description: str = "A custom test skill", body: str = "Do it.") -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n", encoding="utf-8"
    )
    return skill_dir


class TestBundledSkills:
    def test_ships_the_25_addy_skills_plus_human_in_the_loop(self, skills_ws):
        names = {item["name"] for item in sm.list_all_skills()}
        assert len(ADDY_SKILLS) == 25
        assert names == BUNDLED_SKILLS

    @pytest.mark.parametrize("skill_name", sorted(BUNDLED_SKILLS))
    def test_every_bundled_skill_passes_spec_validation(self, skill_name):
        assert validate_skill_directory(sm.BUNDLED_SKILLS_ROOT / skill_name) == []

    def test_shared_references_folder_is_not_loaded_as_a_skill(self, skills_ws):
        assert (sm.BUNDLED_SKILLS_ROOT / "_references").is_dir()
        names = {item["name"] for item in sm.list_all_skills()}
        assert "_references" not in names

    def test_human_in_the_loop_skill_documents_agno_confirmation(self, skills_ws):
        detail = sm.get_skill_detail("human-in-the-loop")
        assert detail is not None
        assert detail["valid"] is True
        assert detail["source"] == "bundled"
        assert "hitl-patterns.md" in detail["references"]
        assert "requires_confirmation" in detail["instructions"]

    def test_skills_with_references_expose_them(self, skills_ws):
        detail = sm.get_skill_detail("test-driven-development")
        assert "testing-patterns.md" in detail["references"]


class TestListAndInspectTools:
    def test_list_platform_skills_reports_both_roots(self, skills_ws):
        result = st.list_platform_skills()
        assert result["status"] == "ok"
        assert result["count"] == len(BUNDLED_SKILLS)
        assert result["workspace_root"] == str(skills_ws)
        assert skills_ws.is_dir()
        assert {item["source"] for item in result["skills"]} == {"bundled"}
        assert not any(item["overridden"] for item in result["skills"])

    def test_inspect_skill_details_returns_instructions_and_validation(self, skills_ws):
        result = st.inspect_skill_details("code-review-and-quality")
        assert result["status"] == "ok"
        assert result["name"] == "code-review-and-quality"
        assert result["skill"]["valid"] is True
        assert result["validation_errors"] == []
        assert result["instructions"].strip()

    @pytest.mark.parametrize("name", ["does-not-exist", "", "   "])
    def test_inspect_unknown_skill_is_not_found(self, skills_ws, name):
        result = st.inspect_skill_details(name)
        assert result["status"] == "not_found"


class TestCustomSkillAuthoring:
    def test_creates_a_valid_workspace_skill(self, skills_ws):
        result = st.create_or_update_custom_skill(
            skill_name="release-notes",
            description="Draft release notes from merged pull requests",
            instructions="1. Collect merged PRs.\n2. Group by area.",
        )
        assert result["status"] == "ok"
        assert result["valid"] is True
        skill_dir = skills_ws / "release-notes"
        assert validate_skill_directory(skill_dir) == []
        text = (skill_dir / "SKILL.md").read_text()
        assert text.startswith("---\nname: release-notes\n")
        assert "Collect merged PRs." in text

        listed = {item["name"]: item for item in st.list_platform_skills()["skills"]}
        assert listed["release-notes"]["source"] == "workspace"
        assert listed["release-notes"]["overridden"] is False

    def test_normalises_the_skill_name(self, skills_ws):
        result = st.create_or_update_custom_skill(
            skill_name="My  Custom_Skill!",
            description="desc",
            instructions="body",
        )
        assert result["status"] == "ok"
        assert result["skill_name"] == "my-custom-skill"
        assert (skills_ws / "my-custom-skill" / "SKILL.md").is_file()

    def test_writes_references_and_strips_path_components(self, skills_ws):
        result = st.create_or_update_custom_skill(
            skill_name="with-refs",
            description="desc",
            instructions="body",
            references={"guide.md": "# Guide", "../escape.md": "x", ".hidden": "y"},
        )
        assert result["status"] == "ok"
        refs_dir = skills_ws / "with-refs" / "references"
        assert sorted(p.name for p in refs_dir.iterdir()) == ["escape.md", "guide.md"]
        assert not (skills_ws / "escape.md").exists()
        assert sorted(sm.get_skill_detail("with-refs")["references"]) == ["escape.md", "guide.md"]

    def test_workspace_skill_overrides_a_bundled_skill(self, skills_ws):
        st.create_or_update_custom_skill(
            skill_name="code-simplification",
            description="Workspace override of code simplification",
            instructions="override body",
        )
        listed = {item["name"]: item for item in st.list_platform_skills()["skills"]}
        assert listed["code-simplification"]["source"] == "workspace"
        assert listed["code-simplification"]["overridden"] is True
        assert listed["code-simplification"]["description"].startswith("Workspace override")

    def test_update_rewrites_an_existing_skill(self, skills_ws):
        st.create_or_update_custom_skill("notes", "first", "v1")
        st.create_or_update_custom_skill("notes", "second", "v2")
        detail = sm.get_skill_detail("notes")
        assert detail["description"] == "second"
        assert "v2" in detail["instructions"]

    @pytest.mark.parametrize("bad_name", ["", "  ", "../escape", "a/b", "a\\b", "!!!", "x" * 65])
    def test_rejects_invalid_names(self, skills_ws, bad_name):
        result = st.create_or_update_custom_skill(bad_name, "desc", "body")
        assert result["status"] == "error"
        assert not skills_ws.exists() or not any(skills_ws.iterdir())

    @pytest.mark.parametrize("bad_description", ["", "   ", "d" * 1025])
    def test_rejects_invalid_descriptions(self, skills_ws, bad_description):
        result = st.create_or_update_custom_skill("ok-name", bad_description, "body")
        assert result["status"] == "error"
        assert not (skills_ws / "ok-name").exists()

    def test_validation_failure_rolls_back_a_new_skill(self, skills_ws, monkeypatch):
        monkeypatch.setattr(st, "validate_skill_directory", lambda path: ["boom"])
        result = st.create_or_update_custom_skill("rollback-me", "desc", "body")
        assert result["status"] == "error"
        assert result["validation_errors"] == ["boom"]
        assert not (skills_ws / "rollback-me").exists()

    def test_an_invalid_workspace_skill_does_not_break_loading(self, skills_ws):
        write_skill(skills_ws, "good-skill")
        bad = skills_ws / "bad-skill"
        bad.mkdir(parents=True)
        (bad / "SKILL.md").write_text("---\nname: Not-Matching\ndescription: x\n---\nbody\n")

        names = {item["name"] for item in st.list_platform_skills()["skills"]}
        assert "good-skill" in names
        assert "Not-Matching" not in names
        assert names >= BUNDLED_SKILLS

        built = sm.build_skills(["*"])
        assert built is not None
        assert "good-skill" in built.get_skill_names()


class TestDeleteCustomSkill:
    def test_is_exported_as_a_hitl_confirmed_agno_tool(self):
        wrapper = next(
            t for t in st.SKILL_TOOLS if isinstance(t, Function) and t.name == "delete_custom_skill"
        )
        assert wrapper.requires_confirmation is True
        assert inspect.unwrap(wrapper.entrypoint) is st.delete_custom_skill
        assert callable(st.delete_custom_skill)
        assert not isinstance(st.delete_custom_skill, Function)
        assert st.delete_custom_skill.requires_confirmation is True

    def test_deletes_a_workspace_skill_when_called_directly(self, skills_ws):
        write_skill(skills_ws, "temp-skill")
        result = st.delete_custom_skill("temp-skill")
        assert result["status"] == "ok"
        assert not (skills_ws / "temp-skill").exists()
        assert "temp-skill" not in {s["name"] for s in sm.list_all_skills()}

    def test_refuses_to_delete_bundled_skills(self, skills_ws):
        result = st.delete_custom_skill("human-in-the-loop")
        assert result["status"] == "error"
        assert "bundled" in result["error"]
        assert (sm.BUNDLED_SKILLS_ROOT / "human-in-the-loop" / "SKILL.md").is_file()

    def test_deleting_a_workspace_override_restores_the_bundled_skill(self, skills_ws):
        write_skill(skills_ws, "code-simplification", description="override")
        assert sm.get_skill_detail("code-simplification")["source"] == "workspace"
        assert st.delete_custom_skill("code-simplification")["status"] == "ok"
        assert sm.get_skill_detail("code-simplification")["source"] == "bundled"

    def test_unknown_skill_is_not_found(self, skills_ws):
        assert st.delete_custom_skill("ghost")["status"] == "not_found"

    @pytest.mark.parametrize("bad_name", ["", "../app", "a/b"])
    def test_rejects_path_like_names(self, skills_ws, bad_name):
        assert st.delete_custom_skill(bad_name)["status"] == "error"


class TestRecommendations:
    def test_testing_task_ranks_tdd_first(self, skills_ws):
        result = st.recommend_skills_for_task("write pytest unit tests with tdd and coverage")
        assert result["status"] == "ok"
        assert result["recommended_skill_names"][0] == "test-driven-development"

    def test_destructive_approval_task_recommends_hitl(self, skills_ws):
        result = st.recommend_skills_for_task("require human approval before a destructive delete")
        assert result["recommended_skill_names"][0] == "human-in-the-loop"

    def test_security_task_recommends_hardening(self, skills_ws):
        result = st.recommend_skills_for_task("audit OWASP vulnerabilities and secrets handling")
        assert "security-and-hardening" in result["recommended_skill_names"][:2]

    def test_respects_top_k(self, skills_ws):
        result = st.recommend_skills_for_task("debug the failing pipeline and fix tests", top_k=2)
        assert result["count"] == 2
        assert len(result["recommendations"]) == 2

    def test_unmatched_task_falls_back_to_foundational_skills(self, skills_ws):
        result = st.recommend_skills_for_task("zzzz qqqq")
        assert result["recommended_skill_names"] == [
            "using-agent-skills",
            "spec-driven-development",
            "planning-and-task-breakdown",
            "test-driven-development",
            "code-review-and-quality",
        ]
        assert all(item["score"] == 1.0 for item in result["recommendations"])

    def test_workspace_skills_are_recommended(self, skills_ws):
        write_skill(skills_ws, "kubernetes-rollouts", description="Plan kubernetes helm rollouts")
        result = st.recommend_skills_for_task("plan a kubernetes helm upgrade")
        top = result["recommendations"][0]
        assert top["name"] == "kubernetes-rollouts"
        assert top["source"] == "workspace"


class TestBuildSkills:
    def test_none_loads_every_skill(self, skills_ws):
        built = sm.build_skills()
        assert isinstance(built, Skills)
        assert set(built.get_skill_names()) == BUNDLED_SKILLS

    @pytest.mark.parametrize("wildcard", [["*"], ["all"], ["ALL"]])
    def test_wildcards_load_every_skill(self, skills_ws, wildcard):
        assert set(sm.build_skills(wildcard).get_skill_names()) == BUNDLED_SKILLS

    def test_filters_by_name(self, skills_ws):
        built = sm.build_skills(["human-in-the-loop", "test-driven-development"])
        assert sorted(built.get_skill_names()) == ["human-in-the-loop", "test-driven-development"]

    @pytest.mark.parametrize("names", [[], ["", "  "], ["no-such-skill"]])
    def test_empty_or_unknown_selection_returns_none(self, skills_ws, names):
        assert sm.build_skills(names) is None

    def test_includes_workspace_skills(self, skills_ws):
        write_skill(skills_ws, "team-conventions")
        built = sm.build_skills(["team-conventions", "code-review-and-quality"])
        assert sorted(built.get_skill_names()) == ["code-review-and-quality", "team-conventions"]

    def test_workspace_definition_wins_over_bundled(self, skills_ws):
        write_skill(skills_ws, "code-review-and-quality", description="override")
        skill = sm.build_skills(["code-review-and-quality"]).get_skill("code-review-and-quality")
        assert skill.description == "override"
        assert Path(skill.source_path).parent == skills_ws

    def test_dot_and_underscore_folders_are_skipped(self, skills_ws):
        write_skill(skills_ws, ".upstream_agent_skills")
        write_skill(skills_ws, "_scratch")
        assert set(sm.build_skills().get_skill_names()) == BUNDLED_SKILLS

    def test_workspace_root_follows_the_settings_workspace(self, workspace):
        write_skill(workspace / "skills", "from-settings-root")
        assert "from-settings-root" in sm.build_skills().get_skill_names()


class TestAttachSkillsToDynamicAgents:
    @pytest.fixture()
    def no_sync(self, monkeypatch):
        from app.tools import platform_tools

        monkeypatch.setattr(platform_tools, "sync_openwebui_models", lambda: {"status": "skipped"})
        return platform_tools

    def test_create_or_update_agent_persists_skills(self, registry_file, skills_ws, no_sync):
        from app.agents.store import get_spec

        result = no_sync.create_or_update_agent(
            agent_id="qa-bot",
            name="QA Bot",
            skills=["test-driven-development", "human-in-the-loop"],
        )
        assert result["status"] == "ok"
        assert result["agent"]["skills"] == ["test-driven-development", "human-in-the-loop"]

        spec = get_spec("qa-bot")
        built = sm.build_skills(spec.skills)
        assert sorted(built.get_skill_names()) == ["human-in-the-loop", "test-driven-development"]

    def test_update_without_skills_keeps_existing_bindings(self, registry_file, no_sync):
        from app.agents.store import get_spec

        no_sync.create_or_update_agent(agent_id="qa-bot", name="QA", skills=["*"])
        no_sync.create_or_update_agent(agent_id="qa-bot", name="QA v2")
        assert get_spec("qa-bot").skills == ["*"]
        assert get_spec("qa-bot").name == "QA v2"

    def test_update_can_replace_skill_bindings(self, registry_file, no_sync):
        from app.agents.store import get_spec

        no_sync.create_or_update_agent(agent_id="qa-bot", name="QA", skills=["code-review-and-quality"])
        no_sync.create_or_update_agent(agent_id="qa-bot", name="QA", skills=["security-and-hardening"])
        assert get_spec("qa-bot").skills == ["security-and-hardening"]


class TestSyncAddyAgentSkills:
    @staticmethod
    def _fake_upstream(dest: Path) -> None:
        (dest / ".git").mkdir(parents=True)
        skills_dir = dest / "skills"
        write_skill(skills_dir, "fresh-skill", body="See testing-patterns.md for details.")
        write_skill(skills_dir, "code-review-and-quality", description="Upstream code review update")
        bad = skills_dir / "broken-skill"
        bad.mkdir()
        (bad / "SKILL.md").write_text("no frontmatter here")
        write_skill(skills_dir, "_private")
        (dest / "references").mkdir()
        (dest / "references" / "testing-patterns.md").write_text("# Testing patterns")
        (dest / "references" / "unused.md").write_text("# Unused")

    def test_clones_validates_and_copies_skills(self, skills_ws, monkeypatch):
        commands: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            commands.append(cmd)
            if cmd[:2] == ["git", "clone"]:
                self._fake_upstream(Path(cmd[-1]))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(st.subprocess, "run", fake_run)
        result = st.sync_addy_agent_skills(repo_url="https://example.invalid/skills.git", branch="dev")

        assert result["status"] == "ok"
        assert commands[0][:2] == ["git", "clone"]
        assert "dev" in commands[0]
        assert sorted(result["skills"]) == ["code-review-and-quality", "fresh-skill"]
        assert result["synced_count"] == 2
        assert set(result["invalid_skills"]) == {"broken-skill"}

        refs = skills_ws / "fresh-skill" / "references"
        assert (refs / "testing-patterns.md").read_text() == "# Testing patterns"
        assert not (refs / "unused.md").exists()
        assert not (skills_ws / "_private").exists()

        detail = sm.get_skill_detail("code-review-and-quality")
        assert detail["source"] == "workspace"
        assert detail["overridden"] is True

    def test_existing_checkout_is_fetched_and_reset(self, skills_ws, monkeypatch):
        upstream = skills_ws / ".upstream_agent_skills"
        self._fake_upstream(upstream)
        commands: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            commands.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(st.subprocess, "run", fake_run)
        result = st.sync_addy_agent_skills()

        assert result["status"] == "ok"
        assert [c[3] for c in commands] == ["fetch", "reset"]
        assert "fresh-skill" in result["skills"]

    def test_git_failure_is_reported_not_raised(self, skills_ws, monkeypatch):
        def failing_run(cmd, **kwargs):
            raise subprocess.CalledProcessError(128, cmd, stderr="fatal: repository not found")

        monkeypatch.setattr(st.subprocess, "run", failing_run)
        result = st.sync_addy_agent_skills(repo_url="https://example.invalid/nope.git")
        assert result["status"] == "error"
        assert "nope.git" in result["error"]
