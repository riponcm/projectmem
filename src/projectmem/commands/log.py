from __future__ import annotations

from pathlib import Path

import typer

from projectmem.models import Event
from projectmem.storage import (
    append_event,
    get_git_commit,
    next_issue_id,
    read_events,
    write_current_issue,
)
from projectmem.summary import regenerate_summary


def run(
    text: str,
    location: str | None = None,
    root: Path | None = None,
) -> Event:
    """Open a new issue. Returns the created Event.

    Side-effect: marks this issue as the project's current/active issue so that
    subsequent `pjm attempt` calls without an explicit `--issue` attach here.
    """
    events = read_events(root)
    issue_id = next_issue_id(events)
    event = Event(
        type="issue",
        issue_id=issue_id,
        summary=text,
        git_commit=get_git_commit(root),
        location=location,
    )
    append_event(event, root)
    write_current_issue(issue_id, root)
    regenerate_summary(root)
    typer.echo(f"Logged issue #{issue_id}")
    return event
