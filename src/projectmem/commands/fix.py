from __future__ import annotations

from pathlib import Path

import typer

from projectmem.models import Event
from projectmem.storage import (
    ProjectMemError,
    append_event,
    clear_current_issue,
    current_issue_id,
    get_git_commit,
    read_current_issue,
    read_events,
)
from projectmem.summary import regenerate_summary


def _normalize_issue_id(issue_id: str | None) -> str | None:
    """Normalize issue IDs so `1`, `001`, and `0001` all become `0001`."""
    if issue_id is None:
        return None

    cleaned = issue_id.strip().lstrip("#")
    if not cleaned:
        return None
    if cleaned.isdigit():
        return cleaned.zfill(4)
    return cleaned


def _issue_exists(events: list[Event], issue_id: str) -> bool:
    """Return True if an issue event exists for the requested issue ID."""
    return any(event.type == "issue" and event.issue_id == issue_id for event in events)


def run(
    text: str,
    location: str | None = None,
    issue: str | None = None,
    root: Path | None = None,
) -> Event:
    """Close an issue with a fix. Returns the created fix Event.

    When `issue` is omitted, this preserves the existing behavior:
    close the current issue and clear the current-issue marker.

    When `issue` is provided, the fix is attached to that specific issue.
    The current-issue marker is only cleared if it points at the same issue.
    """
    events = read_events(root)
    requested_issue_id = _normalize_issue_id(issue)
    active_issue_id = read_current_issue(root) or current_issue_id(events)

    if requested_issue_id is not None:
        issue_id = requested_issue_id
        if not _issue_exists(events, issue_id):
            raise ProjectMemError(
                f"Issue #{issue_id} was not found. "
                "Run `pjm search <query>` or `pjm brief` to find the issue ID."
            )
    else:
        issue_id = active_issue_id

    if issue_id is None:
        raise ProjectMemError("No open issue found. Run `pjm log <text>` first.")

    event = Event(
        type="fix",
        issue_id=issue_id,
        summary=text,
        git_commit=get_git_commit(root),
        location=location,
    )
    append_event(event, root)

    # Preserve old behavior when no specific issue was requested. For targeted
    # fixes, only clear the active marker if it matches the issue being fixed.
    if requested_issue_id is None or active_issue_id == issue_id:
        clear_current_issue(root)

    regenerate_summary(root)
    typer.echo(f"Fixed issue #{issue_id}")
    return event
