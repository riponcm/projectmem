from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from projectmem.commands import backfill
from projectmem.commands.init import run as init_run
from projectmem.storage import read_events


def _commit(repo: Path, message: str, author: str = "t") -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", f"user.name={author}",
         "commit", "-q", "--allow-empty", "-m", message],
        cwd=repo, check=True,
    )


def _init(repo: Path, **kw: bool) -> None:
    init_run(root=repo, no_hooks=True, no_claude_md=True, no_watch=True,
             no_global=True, no_mcp_config=True, **kw)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "home" / ".pjm"))
    root = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _commit(root, "chore: start")
    _commit(root, "feat: one | two | three", author="Ann | Ops")
    _commit(root, "fix: login | logout race")
    return root


def test_backfill_keeps_commits_with_a_pipe_in_the_subject(repo: Path) -> None:
    """A "|" in a subject or author name used to abort backfill (#39)."""
    _init(repo, no_backfill=True)

    backfill.run(limit=20, root=repo)

    events = read_events(repo)
    summaries = [e.summary for e in events if e.git_commit and e.type != "issue"]
    assert summaries == ["chore: start", "feat: one | two | three", "fix: login | logout race"]
    feat = next(e for e in events if e.summary == "feat: one | two | three")
    assert feat.notes == "Auto-backfilled from git commit by Ann | Ops"


def test_init_imports_history_with_a_pipe_in_a_subject(repo: Path) -> None:
    """`pjm init` hides backfill errors, so the crash imported no history."""
    _init(repo)

    assert len([e for e in read_events(repo) if e.git_commit and e.type != "issue"]) == 3
