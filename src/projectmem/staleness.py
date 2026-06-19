"""Stale-memory detection (0.1.4) — judgment, not decay.

Other memory tools silently down-rank or delete old memories; projectmem
never deletes. Instead it cross-references each decision/fix/note against
the git history of the file it cites: if the file has changed substantially
since the event was logged, the event is *flagged* as possibly stale and a
human decides — confirm it still holds, or retire it with
``pjm decision "..." --supersedes <id>``.

Detection is deliberately cheap and deterministic: one ``git log`` count per
distinct (file, oldest-event) pair, no embeddings, no daemon. Events whose
referenced file no longer exists are flagged too (strongest staleness
signal of all).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from projectmem.models import Event, superseded_ids

# A memory is "possibly stale" once its file changed in this many commits
# after the event was logged. 3 tracks the precheck block threshold — one
# rewrite is normal drift, three separate changes mean the file moved on.
STALE_COMMIT_THRESHOLD = 3

# Event types that assert something durable about a file. Attempts are
# excluded: a failed attempt is a historical fact, not a claim about the
# file's current shape — it cannot go stale.
_STALE_CHECKED_TYPES = ("decision", "fix", "note")


def location_file(event: Event) -> str | None:
    """File part of an event's location (``src/auth.py:42`` -> ``src/auth.py``)."""
    if not event.location:
        return None
    file_part = event.location.split(":")[0].strip()
    # Locations like "class AuthHandler" or "deploy pipeline" aren't paths.
    if not file_part or ("/" not in file_part and "." not in file_part):
        return None
    return file_part


def commits_touching_since(
    file_path: str, since_iso: str, root: Path | None = None
) -> int | None:
    """Count commits that touched `file_path` after `since_iso`.

    Returns None when git is unavailable / not a repo — callers must treat
    that as "cannot judge", never as "stale".
    """
    try:
        result = subprocess.run(
            ["git", "log", f"--since={since_iso}", "--oneline", "--", file_path],
            cwd=root or Path.cwd(),
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return sum(1 for line in result.stdout.splitlines() if line.strip())


def find_stale_events(
    events: list[Event],
    root: Path | None = None,
    threshold: int = STALE_COMMIT_THRESHOLD,
) -> list[dict]:
    """Flag live decisions/fixes/notes whose cited file has moved on.

    Returns dicts: ``{event, file, commits_since}`` — ``commits_since`` is
    -1 when the cited file no longer exists (deleted/renamed), which is
    reported as its own, stronger staleness reason. Superseded events are
    skipped: they are already retired, flagging them again is noise.
    """
    root_path = root or Path.cwd()
    retired = superseded_ids(events)
    flagged: list[dict] = []
    # Memoize git calls per (file, timestamp) — many events share a file.
    counts: dict[tuple[str, str], int | None] = {}

    for event in events:
        if event.type not in _STALE_CHECKED_TYPES or event.id in retired:
            continue
        file_path = location_file(event)
        if not file_path:
            continue
        if not (root_path / file_path).exists():
            flagged.append({"event": event, "file": file_path, "commits_since": -1})
            continue
        key = (file_path, event.timestamp)
        if key not in counts:
            counts[key] = commits_touching_since(file_path, event.timestamp, root_path)
        count = counts[key]
        if count is not None and count >= threshold:
            flagged.append({"event": event, "file": file_path, "commits_since": count})
    return flagged


def stale_label(item: dict) -> str:
    """One-line human description of a stale flag."""
    event: Event = item["event"]
    if item["commits_since"] == -1:
        reason = f"cited file {item['file']} no longer exists"
    else:
        reason = f"predates {item['commits_since']} commits to {item['file']}"
    return (
        f"{event.type} [{event.id}] \"{event.summary[:70]}\" — {reason}. "
        f"Confirm, or retire with: pjm decision \"...\" --supersedes {event.id}"
    )
