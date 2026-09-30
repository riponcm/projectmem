"""Internal auto-capture command called by git hooks.

This is NOT a user-facing command.  Git hooks invoke it as:
    pjm _auto-capture commit
    pjm _auto-capture merge

It reads the latest git state, classifies the event, and appends an
auto-captured event to events.jsonl.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import typer

from projectmem.models import Event
from projectmem.storage import (
    MEM_DIR,
    append_event,
    get_git_commit,
    read_events,
)
from projectmem.summary import regenerate_summary


# ── Classification Patterns ──────────────────────────────────────────
# Order matters — first match wins. Patterns are tested case-insensitively
# against the full commit message.

COMMIT_PATTERNS: list[dict[str, Any]] = [
    {
        "name": "revert",
        "pattern": re.compile(r"^revert\b|^Revert\b", re.IGNORECASE),
        "event_type": "attempt",
        "outcome": "failed",
        "prefix": "Reverted",
        "confidence": "high",
        "capture_source": "git_post_commit",
    },
    {
        "name": "fix",
        "pattern": re.compile(
            r"^fix[\s(:]|^hotfix[\s(:]|^bugfix[\s(:]|^patch[\s(:]", re.IGNORECASE
        ),
        "event_type": "fix",
        "outcome": None,
        "prefix": "Fix",
        "confidence": "high",
        "capture_source": "git_post_commit",
    },
    {
        "name": "breaking",
        "pattern": re.compile(r"BREAKING[\s_-]?CHANGE|^break[\s(:]", re.IGNORECASE),
        "event_type": "decision",
        "outcome": None,
        "prefix": "Breaking change",
        "confidence": "high",
        "capture_source": "git_post_commit",
    },
    {
        "name": "feature",
        "pattern": re.compile(r"^feat[\s(:]|^feature[\s(:]|^add[\s(:]", re.IGNORECASE),
        "event_type": "note",
        "outcome": None,
        "prefix": "New feature",
        "confidence": "medium",
        "capture_source": "git_post_commit",
    },
    {
        "name": "refactor",
        "pattern": re.compile(
            r"^refactor[\s(:]|^cleanup[\s(:]|^reorganize|^restructure",
            re.IGNORECASE,
        ),
        "event_type": "decision",
        "outcome": None,
        "prefix": "Refactor",
        "confidence": "medium",
        "capture_source": "git_post_commit",
    },
    {
        "name": "docs",
        "pattern": re.compile(r"^docs?[\s(:]|^readme|^changelog", re.IGNORECASE),
        "event_type": "note",
        "outcome": None,
        "prefix": "Documentation",
        "confidence": "low",
        "capture_source": "git_post_commit",
    },
    {
        "name": "test",
        "pattern": re.compile(r"^test[\s(:]|^tests?[\s(:]|^spec[\s(:]", re.IGNORECASE),
        "event_type": "note",
        "outcome": None,
        "prefix": "Tests",
        "confidence": "low",
        "capture_source": "git_post_commit",
    },
]

# Minimum confidence to actually log (skip "low" by default to reduce noise)
MIN_CONFIDENCE = "medium"
CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}


def run(trigger: str = "commit", root: Path | None = None) -> None:
    """Classify the latest git action and log it as an auto-captured event."""
    root_path = root or Path.cwd()

    # Guard: only run if .projectmem exists
    if not (root_path / MEM_DIR).exists():
        return

    # Check auto-capture config
    config_path = root_path / MEM_DIR / "config.toml"
    if config_path.exists():
        config_text = config_path.read_text(encoding="utf-8")
        if "auto_capture = false" in config_text:
            return

    if trigger == "commit":
        _capture_commit(root_path)
    elif trigger == "merge":
        _capture_merge(root_path)


def _pick_location(files: list[str]) -> str | None:
    """Pick the file an auto-captured event is about.

    ``git diff-tree --name-only`` lists changed paths in lexicographic order,
    and ".projectmem/" sorts ahead of almost every real source path because
    of the leading dot. Using ``files[0]`` therefore pinned the location to a
    memory file whenever a commit also rewrote one — and the project's own
    workflow regenerates ``summary.md`` on every recorded event, so that was
    most commits. Staleness checks then count every later commit touching the
    memory file and report valid memories as possibly stale (issue #20).

    The event should cite the code the commit describes: the first changed
    path outside the memory directory. A commit that only touches
    projectmem's own files has no source file to cite, so fall back to the
    first path to stay debuggable.
    """
    if not files:
        return None
    for file in files:
        if file == MEM_DIR or file.startswith(MEM_DIR + "/"):
            continue
        return file
    return files[0]


def _capture_commit(root: Path) -> None:
    """Classify and capture a git commit."""
    msg = _git_last_message(root)
    if not msg:
        return

    files = _git_last_changed_files(root)
    commit_hash = get_git_commit(root)

    # Deduplicate: don't re-log if this commit is already captured
    try:
        existing = read_events(root)
        existing_commits = {e.git_commit for e in existing if e.git_commit}
        if commit_hash and commit_hash in existing_commits:
            return
    except Exception:
        pass  # If events can't be read, proceed anyway

    # Classify
    matched = _classify_message(msg)
    if not matched:
        return

    # Check confidence threshold
    if CONFIDENCE_RANK.get(matched["confidence"], 0) < CONFIDENCE_RANK.get(
        MIN_CONFIDENCE, 2
    ):
        return

    # Build summary
    first_line = msg.strip().split("\n")[0][:120]
    summary = f"{matched['prefix']}: {first_line}"

    event = Event(
        type=matched["event_type"],
        summary=summary,
        outcome=matched["outcome"],
        files=files[:10],  # Cap at 10 files
        git_commit=commit_hash,
        location=_pick_location(files),
        auto_captured=True,
        capture_source=matched["capture_source"],
        capture_confidence=matched["confidence"],
        git_message=first_line,
        command="auto-capture",
    )

    try:
        append_event(event, root)
        regenerate_summary(root)
        # Color output for terminal feedback
        colors = {
            "attempt": "\033[0;33m",  # yellow for reverts
            "fix": "\033[0;32m",      # green for fixes
            "decision": "\033[0;31m", # red for breaking/decisions
            "note": "\033[0;36m",     # cyan for features/notes
        }
        color = colors.get(event.type, "\033[0;37m")
        typer.echo(
            f"{color}[projectmem] Auto-captured: {summary}\033[0m"
        )
    except Exception:
        pass  # Silent failure — never block the developer's workflow


def _capture_merge(root: Path) -> None:
    """Capture a branch merge event."""
    msg = _git_last_message(root)
    if not msg:
        return

    commit_hash = get_git_commit(root)
    first_line = msg.strip().split("\n")[0][:120]

    # Deduplicate
    try:
        existing = read_events(root)
        existing_commits = {e.git_commit for e in existing if e.git_commit}
        if commit_hash and commit_hash in existing_commits:
            return
    except Exception:
        pass

    event = Event(
        type="note",
        summary=f"Merge: {first_line}",
        git_commit=commit_hash,
        location=None,
        auto_captured=True,
        capture_source="git_post_merge",
        capture_confidence="high",
        git_message=first_line,
        command="auto-capture",
    )

    try:
        append_event(event, root)
        regenerate_summary(root)
        typer.echo(
            f"\033[0;35m[projectmem] Auto-captured merge: {first_line}\033[0m"
        )
    except Exception:
        pass


def _classify_message(message: str) -> dict[str, Any] | None:
    """Match a commit message against classification patterns."""
    for pattern in COMMIT_PATTERNS:
        if pattern["pattern"].search(message):
            return pattern
    return None


def _git_last_message(root: Path) -> str | None:
    """Get the most recent commit message."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--pretty=%B"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() or None
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def _git_last_changed_files(root: Path) -> list[str]:
    """Get files changed in the most recent commit."""
    try:
        result = subprocess.run(
            ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        files = [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
        return files
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []
