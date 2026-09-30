"""Regression tests for auto-capture location selection (issue #20).

`git diff-tree --name-only` lists changed paths in lexicographic order, and
".projectmem/" sorts ahead of real source paths because of the leading dot.
``_capture_commit`` used ``files[0]`` as the event location, so any commit
that also rewrote a memory file (the regenerated ``summary.md`` is committed
constantly by the project's own workflow) got a memory file as its location.
Staleness checks then count every later commit touching that file and report
valid memories as possibly stale.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from projectmem.commands import auto_capture


class TestPickLocation:
    """The pure selection helper."""

    def test_prefers_source_file_over_memory_files(self) -> None:
        # git orders ".projectmem/..." before "apps/..." — the old files[0]
        # behaviour would have picked the summary file.
        files = [
            ".projectmem/summary.md",
            ".projectmem/PROJECT_MAP.md",
            "apps/heating/src/main.ts",
            "apps/heating/src/scopes.ts",
        ]
        assert auto_capture._pick_location(files) == "apps/heating/src/main.ts"

    def test_memory_file_in_middle_does_not_shadow_source(self) -> None:
        files = [
            "src/api/routes.py",
            ".projectmem/issues/42.json",
            "src/api/handlers.py",
        ]
        assert auto_capture._pick_location(files) == "src/api/routes.py"

    def test_memory_only_commit_falls_back_to_first(self) -> None:
        # A commit that only touches projectmem's own files has no source
        # file to cite; keep pointing at something debuggable.
        files = [".projectmem/summary.md", ".projectmem/plan.md"]
        assert auto_capture._pick_location(files) == ".projectmem/summary.md"

    def test_empty_list_returns_none(self) -> None:
        assert auto_capture._pick_location([]) is None


class TestCaptureCommitLocation:
    """End-to-end: a captured commit cites the code it describes."""

    def test_location_is_source_file_not_memory_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[dict] = []

        def fake_message(root) -> str:
            return "fix: check the token by capability, not by scope name"

        # git's lexicographic ordering: the dot-directory sorts first.
        def fake_files(root) -> list[str]:
            return [
                ".projectmem/summary.md",
                "apps/heating/src/scopes.test.ts",
                "apps/heating/src/scopes.ts",
            ]

        def fake_append(event, root):
            captured.append(event.to_dict())

        monkeypatch.setattr(auto_capture, "_git_last_message", fake_message)
        monkeypatch.setattr(auto_capture, "_git_last_changed_files", fake_files)
        monkeypatch.setattr(auto_capture, "get_git_commit", lambda root: "abc123")
        monkeypatch.setattr(auto_capture, "read_events", lambda root: [])
        monkeypatch.setattr(auto_capture, "append_event", fake_append)
        monkeypatch.setattr(auto_capture, "regenerate_summary", lambda root: None)

        auto_capture._capture_commit(tmp_path)

        assert len(captured) == 1
        event = captured[0]
        # The event describes the code the commit changed, not the memory
        # file that the post-commit hook regenerated alongside it.
        assert event["location"] == "apps/heating/src/scopes.test.ts"
        assert event["auto_captured"] is True

    def test_memory_only_commit_still_captured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[dict] = []

        monkeypatch.setattr(auto_capture, "_git_last_message", lambda root: "fix: refresh summary")
        monkeypatch.setattr(
            auto_capture, "_git_last_changed_files", lambda root: [".projectmem/summary.md"]
        )
        monkeypatch.setattr(auto_capture, "get_git_commit", lambda root: "def456")
        monkeypatch.setattr(auto_capture, "read_events", lambda root: [])
        monkeypatch.setattr(
            auto_capture,
            "append_event",
            lambda event, root: captured.append(event.to_dict()),
        )
        monkeypatch.setattr(auto_capture, "regenerate_summary", lambda root: None)

        auto_capture._capture_commit(tmp_path)

        assert len(captured) == 1
        assert captured[0]["location"] == ".projectmem/summary.md"
