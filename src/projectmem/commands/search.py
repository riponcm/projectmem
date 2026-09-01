from __future__ import annotations

from pathlib import Path

import typer

from projectmem.models import superseded_ids
from projectmem.search import search_events
from projectmem.storage import read_events


def run(
    query: str,
    regex: bool = False,
    failed_only: bool = False,
    root: Path | None = None,
) -> None:
    """Plain-text or regex search across events.

    By default this is a case-insensitive substring match. Use ``--regex``
    to enable Python regex syntax — including OR-patterns like
    ``"carousel|favicon"`` (L-027c). ``--failed-only`` narrows the result
    to failed attempts — the project's catalogue of dead ends.
    """
    matches = search_events(query, regex=regex)
    if failed_only:
        matches = [
            event
            for event in matches
            if event.type == "attempt" and event.outcome == "failed"
        ]
    if not matches:
        if not regex and any(ch in query for ch in r"|*?+()[]\\"):
            typer.echo(
                "No matches. (Tip: substring match is the default — "
                "rerun with `--regex` if you intended an OR/regex pattern.)"
            )
        else:
            typer.echo("No matches.")
        return

    retired = superseded_ids(read_events(root))
    for event in matches:
        issue = f" #{event.issue_id}" if event.issue_id else ""
        outcome = f" ({event.outcome})" if event.outcome else ""
        retired_tag = " (superseded)" if event.id in retired else ""
        typer.echo(
            f"{event.timestamp} [{event.id}] {event.type}{issue}{outcome}: "
            f"{event.summary}{retired_tag}"
        )
