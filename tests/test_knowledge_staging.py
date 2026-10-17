"""Staging Open WebUI uploads into the workspace.

These exercise the paths that run with embeddings switched off, which is also
the mode that must stay safe: files are copied and reported honestly, nothing
touches the database. The deduplication tests are the regression cover for
files being routed more than once (Filter, Pipe and bridge all trying).
"""

import pytest

from app.knowledge import knowledge_manager as km
from app.knowledge.paths import UnsafePathError


@pytest.fixture()
def uploads(tmp_path, monkeypatch):
    directory = tmp_path / "openwebui_uploads"
    directory.mkdir()
    monkeypatch.setattr(km, "OPENWEBUI_UPLOADS_DIR", directory)
    return directory


def write_upload(uploads, name, content="hello"):
    path = uploads / name
    path.write_text(content, encoding="utf-8")
    return path


class TestFindUpload:
    def test_matches_on_a_substring(self, uploads, workspace):
        write_upload(uploads, "3f1c9a54-0b1d-4f3e-9a2b-7c8d9e0f1a2b_report.md")
        assert km.find_openwebui_upload("report").name.endswith("report.md")

    def test_is_case_insensitive(self, uploads, workspace):
        write_upload(uploads, "Report.md")
        assert km.find_openwebui_upload("report") is not None

    def test_prefers_the_newest_match(self, uploads, workspace):
        import os
        import time

        old = write_upload(uploads, "a_report.md", "old")
        time.sleep(0.01)
        new = write_upload(uploads, "b_report.md", "new")
        os.utime(old, (1, 1))
        assert km.find_openwebui_upload("report") == new

    def test_returns_none_when_nothing_matches(self, uploads, workspace):
        write_upload(uploads, "report.md")
        assert km.find_openwebui_upload("invoice") is None

    def test_returns_none_when_the_mount_is_missing(self, tmp_path, monkeypatch, workspace):
        monkeypatch.setattr(km, "OPENWEBUI_UPLOADS_DIR", tmp_path / "absent")
        assert km.find_openwebui_upload("anything") is None


class TestStageUpload:
    def test_copies_into_the_target_folder(self, uploads, workspace):
        write_upload(uploads, "3f1c9a54-0b1d-4f3e-9a2b-7c8d9e0f1a2b_Report Q1.md")
        result = km.stage_openwebui_upload("report", "contracts")
        assert result["folder"] == "contracts"
        assert (workspace / "contracts" / "report-q1.md").read_text() == "hello"
        assert result["pgvector_table"] == "kb_contracts"

    def test_reports_that_indexing_is_disabled_rather_than_failing(self, uploads, workspace):
        write_upload(uploads, "report.md")
        result = km.stage_openwebui_upload("report", "sops")
        assert result["indexed"] is False
        assert "EMBEDDING_API_KEY" in result["index_error"]

    def test_second_identical_route_is_deduplicated(self, uploads, workspace):
        write_upload(uploads, "report.md")
        first = km.stage_openwebui_upload("report", "sops")
        second = km.stage_openwebui_upload("report", "sops")
        assert first["deduplicated"] is False
        assert second["deduplicated"] is True

    def test_changed_content_is_copied_again(self, uploads, workspace):
        write_upload(uploads, "report.md", "v1")
        km.stage_openwebui_upload("report", "sops")
        write_upload(uploads, "report.md", "v2")
        result = km.stage_openwebui_upload("report", "sops")
        assert result["deduplicated"] is False
        assert (workspace / "sops" / "report.md").read_text() == "v2"

    def test_defaults_to_the_general_folder(self, uploads, workspace):
        write_upload(uploads, "report.md")
        assert km.stage_openwebui_upload("report")["folder"] == "general"

    def test_rejects_a_traversing_target_folder(self, uploads, workspace):
        write_upload(uploads, "report.md")
        with pytest.raises(UnsafePathError):
            km.stage_openwebui_upload("report", "../../etc")

    def test_missing_upload_raises_a_useful_error(self, uploads, workspace):
        with pytest.raises(FileNotFoundError, match="openwebui_data"):
            km.stage_openwebui_upload("nothing-like-this")

    def test_rejects_an_oversized_upload(self, uploads, workspace, monkeypatch):
        monkeypatch.setattr(km.settings, "max_staged_upload_bytes", 4)
        write_upload(uploads, "report.md", "far too long")
        with pytest.raises(ValueError, match="MAX_STAGED_UPLOAD_BYTES"):
            km.stage_openwebui_upload("report", "sops")

    def test_unsupported_types_are_stored_but_flagged(self, uploads, workspace):
        write_upload(uploads, "archive.zip")
        result = km.stage_openwebui_upload("archive", "general")
        assert result["indexed"] is False
        assert ".zip" in result["index_error"]
        assert (workspace / "general" / "archive.zip").exists()


class TestSaveTextFile:
    def test_writes_into_the_folder(self, workspace):
        result = km.save_text_file("sops", "Checklist.md", "# hi")
        assert (workspace / "sops" / "checklist.md").read_text() == "# hi"
        assert result["bytes"] == 4

    def test_filename_cannot_escape_the_folder(self, workspace):
        km.save_text_file("sops", "../../escape.md", "x")
        assert (workspace / "sops" / "escape.md").exists()
        assert not (workspace.parent / "escape.md").exists()


class TestKnownFolders:
    def test_always_includes_general(self, workspace):
        assert "general" in km.known_folders()

    def test_lists_nested_folders(self, workspace):
        (workspace / "sops" / "2024").mkdir(parents=True)
        assert "sops/2024" in km.known_folders()

    def test_ignores_hidden_directories(self, workspace):
        (workspace / ".hidden").mkdir()
        assert not any(f.startswith(".") for f in km.known_folders())


def test_no_knowledge_bases_are_loaded_without_embeddings(workspace):
    assert km.get_all_knowledge_list() == []
