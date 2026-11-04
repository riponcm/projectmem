from __future__ import annotations

import re
from pathlib import Path

from projectmem.models import Event
from projectmem.storage import RECORD_CHARS, clip_record, read_events


def search_events(
    query: str, regex: bool = False, root: Path | None = None
) -> list[Event]:
    """Search the event log.

    Default mode is case-insensitive substring match against the summary,
    notes, `location`, and `files` array. With ``regex=True`` the query is
    treated as a Python regex (case-insensitive) — useful for OR-patterns
    like ``"carousel|favicon"`` that previously returned `No matches.`
    because the literal pipe character isn't in any event (L-027c).
    Location matching (0.1.4) makes per-file lookups work the way precheck
    does — `pjm search payment.py --failed-only` finds attempts logged
    with `--at payment.py`.
    """
    events = read_events(root)
    if regex:
        try:
            pattern = re.compile(query, re.IGNORECASE)
        except re.error:
            # Bad regex → fall back to literal substring rather than crash.
            return search_events(query, regex=False, root=root)
        return [
            event
            for event in events
            if pattern.search(event.summary)
            or (event.notes and pattern.search(event.notes))
            or (event.location and pattern.search(event.location))
            or any(pattern.search(f) for f in event.files)
        ]
    needle = query.casefold()
    return [
        event
        for event in events
        if needle in event.summary.casefold()
        or (event.notes and needle in event.notes.casefold())
        or (event.location and needle in event.location.casefold())
        or any(needle in file_path.casefold() for file_path in event.files)
    ]


def _flat(text: str) -> str:
    return " ".join(text.split())


def excerpt(text: str, query: str) -> str:
    flat = _flat(text)
    found = flat.casefold().find(query.casefold())
    if len(flat) <= RECORD_CHARS or found == -1 or found + len(query) <= RECORD_CHARS - 3:
        return clip_record(flat)
    start = flat.find(" ", max(0, found - RECORD_CHARS // 3)) + 1
    return "..." + clip_record(flat[start:])


def match_text(event: Event, query: str, full: bool) -> str:
    in_summary = query.casefold() in event.summary.casefold()
    source = event.summary if in_summary or not event.notes else event.notes
    shown = _flat(source) if full else excerpt(source, query)
    return shown if source is event.summary else f"{clip_record(event.summary)} | notes: {shown}"


def match_line(event: Event, query: str, retired: set[str], full: bool = False) -> str:
    issue = f" #{event.issue_id}" if event.issue_id else ""
    outcome = f" ({event.outcome})" if event.outcome else ""
    location = f" @ {_flat(event.location)}" if event.location else ""
    retired_tag = " (superseded)" if event.id in retired else ""
    return (
        f"  [{event.type}{issue}{outcome}] {event.id} {event.timestamp[:10]}: "
        f"{match_text(event, query, full)}{location}{retired_tag}"
    )
