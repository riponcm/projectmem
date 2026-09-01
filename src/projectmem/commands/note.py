from __future__ import annotations

from pathlib import Path

import typer

from projectmem.models import Event
from projectmem.storage import append_event, get_git_commit
from projectmem.summary import regenerate_summary


def run(
    text: str,
    location: str | None = None,
    root: Path | None = None,
) -> Event:
    event = Event(
        type="note",
        summary=text,
        git_commit=get_git_commit(root),
        location=location,
    )
    append_event(event, root)
    regenerate_summary(root)
    typer.echo("Recorded note")
    return event
