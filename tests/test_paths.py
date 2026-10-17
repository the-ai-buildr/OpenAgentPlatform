"""Path containment and naming rules.

The traversal cases here are the regression tests for the string-prefix
containment check in the original stack, where `/app/workspace_files_evil`
passed a `startswith("/app/workspace_files")` test.
"""

import pytest

from app.knowledge.paths import (
    DEFAULT_FOLDER,
    UnsafePathError,
    folder_dir,
    folder_to_table_name,
    is_supported_file,
    normalise_folder,
    relative_folder_for,
    resolve_within,
    safe_filename,
    strip_openwebui_prefix,
)


class TestNormaliseFolder:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, DEFAULT_FOLDER),
            ("", DEFAULT_FOLDER),
            ("   ", DEFAULT_FOLDER),
            ("/", DEFAULT_FOLDER),
            ("///", DEFAULT_FOLDER),
            ("sops", "sops"),
            ("/sops/", "sops"),
            ("SOPs", "sops"),
            ("sops/2024", "sops/2024"),
            ("sops\\2024", "sops/2024"),
            ("./sops", "sops"),
            ("contracts/Acme Corp", "contracts/acme-corp"),
            ("a//b///c", "a/b/c"),
        ],
    )
    def test_normalises(self, raw, expected):
        assert normalise_folder(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        ["..", "../etc", "sops/../../etc", "a/b/../../../c", "~/secrets", "~"],
    )
    def test_rejects_traversal(self, raw):
        with pytest.raises(UnsafePathError):
            normalise_folder(raw)

    def test_rejects_segment_that_sanitises_to_nothing(self):
        with pytest.raises(UnsafePathError):
            normalise_folder("sops/...")


class TestResolveWithin:
    def test_allows_nested_paths(self, tmp_path):
        assert resolve_within(tmp_path, "a/b") == (tmp_path / "a" / "b").resolve()

    def test_allows_the_root_itself(self, tmp_path):
        assert resolve_within(tmp_path, ".") == tmp_path.resolve()

    def test_rejects_parent_escape(self, tmp_path):
        with pytest.raises(UnsafePathError):
            resolve_within(tmp_path / "root", "../outside")

    def test_rejects_sibling_with_shared_prefix(self, tmp_path):
        """The bug a `startswith` check misses."""
        root = tmp_path / "workspace_files"
        root.mkdir()
        (tmp_path / "workspace_files_evil").mkdir()
        with pytest.raises(UnsafePathError):
            resolve_within(root, str(tmp_path / "workspace_files_evil" / "secrets.txt"))

    def test_rejects_absolute_path_outside_root(self, tmp_path):
        with pytest.raises(UnsafePathError):
            resolve_within(tmp_path, "/etc/passwd")

    def test_rejects_symlink_escape(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (root / "link").symlink_to(outside, target_is_directory=True)
        with pytest.raises(UnsafePathError):
            resolve_within(root, "link/../../outside/secret.txt")


class TestFolderDir:
    def test_joins_under_root(self, tmp_path):
        assert folder_dir(tmp_path, "sops/2024") == (tmp_path / "sops" / "2024").resolve()

    def test_rejects_traversal(self, tmp_path):
        with pytest.raises(UnsafePathError):
            folder_dir(tmp_path, "../etc")


class TestTableNaming:
    @pytest.mark.parametrize(
        ("folder", "expected"),
        [
            (None, "kb_general"),
            ("general", "kb_general"),
            ("sops", "kb_sops"),
            ("sops/2024", "kb_sops_2024"),
            ("Contracts/Acme Corp", "kb_contracts_acme_corp"),
            ("agents/my-agent", "kb_agents_my_agent"),
        ],
    )
    def test_names(self, folder, expected):
        assert folder_to_table_name(folder) == expected

    def test_is_deterministic(self):
        assert folder_to_table_name("sops/2024") == folder_to_table_name("/SOPS/2024/")

    def test_stays_within_postgres_identifier_budget(self):
        name = folder_to_table_name("a" * 200)
        assert len(name) <= 60
        assert name.startswith("kb_")

    def test_never_ends_in_underscore(self):
        assert not folder_to_table_name("a" * 58 + "/b").endswith("_")


class TestFilenames:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("3f1c9a54-0b1d-4f3e-9a2b-7c8d9e0f1a2b_report.pdf", "report.pdf"),
            ("report.pdf", "report.pdf"),
            ("not-a-uuid_report.pdf", "not-a-uuid_report.pdf"),
        ],
    )
    def test_strips_openwebui_prefix(self, raw, expected):
        assert strip_openwebui_prefix(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Report Q1.pdf", "report-q1.pdf"),
            ("../../etc/passwd", "passwd"),
            ("dir/sub/file.md", "file.md"),
            ("C:\\temp\\file.md", "file.md"),
        ],
    )
    def test_safe_filename(self, raw, expected):
        assert safe_filename(raw) == expected

    def test_rejects_empty_filename(self):
        with pytest.raises(UnsafePathError):
            safe_filename("...")


class TestSupportedFiles:
    @pytest.mark.parametrize("name", ["a.pdf", "a.TXT", "a.md", "a.csv", "a.json", "a.docx"])
    def test_supported(self, name):
        assert is_supported_file(name)

    @pytest.mark.parametrize("name", ["a.exe", "a.png", "a", "a.pdf.exe"])
    def test_unsupported(self, name):
        assert not is_supported_file(name)


class TestRelativeFolderFor:
    def test_root_files_belong_to_general(self, tmp_path):
        (tmp_path / "a.md").write_text("x")
        assert relative_folder_for(tmp_path, tmp_path / "a.md") == DEFAULT_FOLDER

    def test_nested_files_report_their_folder(self, tmp_path):
        nested = tmp_path / "sops" / "2024"
        nested.mkdir(parents=True)
        (nested / "a.md").write_text("x")
        assert relative_folder_for(tmp_path, nested / "a.md") == "sops/2024"

    def test_rejects_files_outside_the_root(self, tmp_path):
        outside = tmp_path.parent / "outside.md"
        outside.write_text("x")
        with pytest.raises(UnsafePathError):
            relative_folder_for(tmp_path, outside)
