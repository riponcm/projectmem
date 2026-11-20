from __future__ import annotations

from datetime import datetime, timedelta, timezone

from projectmem.commands.precheck import _analyze_files
from projectmem.models import Event
from projectmem.storage import initialize


def _project(tmp_path):
    initialize(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "auth.py").write_text("x = 1\n", encoding="utf-8")
    return tmp_path


def _days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _lesson_lines(warnings) -> list[str]:
    return [
        detail
        for warning in warnings
        if warning["type"] == "failed_attempts"
        for detail in warning["details"]
    ]


def test_attempt_without_location_inherits_its_issue_file(tmp_path):
    root = _project(tmp_path)
    events = [
        Event(type="issue", issue_id="0001", summary="token refresh race", location="src/auth.py"),
        Event(type="attempt", issue_id="0001", outcome="failed", summary="wrapped refresh in a lock"),
    ]

    lines = _lesson_lines(_analyze_files(["src/auth.py"], events, root=root))

    assert any("wrapped refresh in a lock" in line for line in lines)


def test_partial_attempt_is_a_lesson(tmp_path):
    root = _project(tmp_path)
    events = [
        Event(type="issue", issue_id="0001", summary="token refresh race", location="src/auth.py"),
        Event(type="attempt", issue_id="0001", outcome="partial", summary="retry halves the failures", location="src/auth.py"),
    ]

    lines = _lesson_lines(_analyze_files(["src/auth.py"], events, root=root))

    assert any("retry halves the failures" in line for line in lines)


def test_failure_older_than_thirty_days_still_warns(tmp_path):
    root = _project(tmp_path)
    events = [
        Event(type="issue", issue_id="0001", summary="token refresh race", location="src/auth.py", timestamp=_days_ago(60)),
        Event(type="attempt", issue_id="0001", outcome="failed", summary="pinned the clock skew", location="src/auth.py", timestamp=_days_ago(60)),
    ]

    lines = _lesson_lines(_analyze_files(["src/auth.py"], events, root=root))

    assert any("pinned the clock skew" in line for line in lines)


def test_a_worked_attempt_is_not_a_lesson(tmp_path):
    root = _project(tmp_path)
    events = [
        Event(type="issue", issue_id="0001", summary="token refresh race", location="src/auth.py"),
        Event(type="attempt", issue_id="0001", outcome="worked", summary="serialized refresh per user", location="src/auth.py"),
    ]

    assert not _lesson_lines(_analyze_files(["src/auth.py"], events, root=root))


def _lesson_warning(warnings):
    return next(w for w in warnings if w["type"] == "failed_attempts")


def test_lesson_title_counts_partial_attempts_separately(tmp_path):
    root = _project(tmp_path)
    events = [
        Event(type="issue", issue_id="0001", summary="token refresh race", location="src/auth.py"),
        Event(type="attempt", issue_id="0001", outcome="failed", summary="lock", location="src/auth.py"),
        Event(type="attempt", issue_id="0001", outcome="partial", summary="retry", location="src/auth.py"),
    ]

    title = _lesson_warning(_analyze_files(["src/auth.py"], events, root=root))["title"]

    assert title.startswith("What already failed here (2 attempts, 1 only partly worked)")


def test_overflow_names_the_issues_of_hidden_lessons(tmp_path):
    root = _project(tmp_path)
    events = [
        Event(type="issue", issue_id="0001", summary="old race", location="src/auth.py"),
        Event(type="attempt", issue_id="0001", outcome="partial", summary="old a", location="src/auth.py"),
        Event(type="attempt", issue_id="0001", outcome="failed", summary="old b", location="src/auth.py"),
        Event(type="issue", issue_id="0002", summary="new race", location="src/auth.py"),
        *[
            Event(type="attempt", issue_id="0002", outcome="failed", summary=f"new {i}", location="src/auth.py")
            for i in range(3)
        ],
    ]

    details = _lesson_warning(_analyze_files(["src/auth.py"], events, root=root))["details"]

    assert details[-1].strip() == "... and 2 more, from #0001 (get_issue(issue_id))"


def test_precheck_files_analyzes_all_paths_in_one_pass(tmp_path, monkeypatch):
    from projectmem import mcp_server
    from projectmem.commands import precheck

    root = _project(tmp_path)
    (root / "src" / "db.py").write_text("x = 1\n", encoding="utf-8")
    calls = []
    analyze = precheck._analyze_files

    def counting(files, events, root=None):
        calls.append(list(files))
        return analyze(files, events, root=root)

    monkeypatch.setattr(precheck, "_analyze_files", counting)
    monkeypatch.setattr(mcp_server, "_root_for", lambda project: root)

    text = mcp_server.precheck_files(["src/auth.py", "./src/db.py"])

    assert calls == [["src/auth.py", "./src/db.py"]]
    assert "src/auth.py: no warnings" in text
    assert "./src/db.py: no warnings" in text
