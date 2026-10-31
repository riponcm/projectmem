from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from projectmem.models import Event, superseded_ids
from projectmem.storage import (
    RECORD_CHARS,
    issues_dir,
    project_map_path,
    read_events,
    summary_path,
)


# Phrases that mean "this is still placeholder content, treat as not-yet-set"
# (L-037). Used by both `extract_project_purpose` and
# `extract_project_purpose_from_map` so the regenerator doesn't keep echoing
# the init placeholder back into summary.md forever.
_PLACEHOLDER_PHRASES = (
    "Not described yet.",
    "Short description of what the project does.",
    "Replace this placeholder",
    "Status: not created yet",
    "This file should be created by the first AI assistant",
)


# How much of each growing section summary.md shows (#19). Notes keep their
# latest-10 window. Open issues are never dropped: past OPEN_IN_FULL they are
# still listed, by id only, so a neglected backlog cannot regrow the summary.
RECENT_FIXED_ISSUES = 10
RECENT_DECISIONS = 15
OPEN_IN_FULL = 20

def _clip(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= RECORD_CHARS:
        return text
    head = text[: RECORD_CHARS - 3]
    boundary = head.rfind(" ", RECORD_CHARS // 2 + 1)
    if boundary != -1:
        head = head[:boundary]
    return head.rstrip(" ,;:") + "..."


def _loc(location: str | None) -> str:
    return f" [{' '.join(location.split())}]" if location else ""


def _issue_sort_key(item: tuple[str, list[Event]]) -> tuple[int, str]:
    """Order issue ids numerically: as strings, "10000" sorts before "9999"."""
    issue_id = item[0]
    return (int(issue_id) if issue_id.isdecimal() else -1, issue_id)


def _issue_lines(
    issue_id: str, issue: Event, issue_events: list[Event], fix: Event | None
) -> list[str]:
    issue_loc = _loc(issue.location)
    if fix:
        fixed_loc = issue_loc or _loc(fix.location)
        return [f"- [DONE] #{issue_id} {_clip(issue.summary)}{fixed_loc} (fixed)"]
    lines = [f"- [OPEN] #{issue_id} {_clip(issue.summary)}{issue_loc} (open)"]
    # Surface non-worked attempts. Both `failed` and `partial` outcomes
    # encode lessons the next session needs — dropping `partial` (L-027b)
    # would let an AI repeat work that already got 80% of the way there.
    lessons = [
        event
        for event in issue_events
        if event.type == "attempt" and event.outcome in ("failed", "partial")
    ]
    label = {"failed": "Failed attempt", "partial": "Partial attempt"}
    for lesson_event in lessons[-3:]:
        tag = label.get(lesson_event.outcome or "failed", "Attempt")
        lines.append(f"  - {tag}: {_clip(lesson_event.summary)}{_loc(lesson_event.location)}")
    return lines


def _fix_for(issue_events: list[Event]) -> Event | None:
    return next((event for event in reversed(issue_events) if event.type == "fix"), None)


def _looks_like_placeholder(text: str) -> bool:
    """True if `text` is empty or contains a known placeholder phrase."""
    if not text:
        return True
    stripped = text.strip()
    if not stripped:
        return True
    return any(phrase in stripped for phrase in _PLACEHOLDER_PHRASES)


def regenerate_summary(root: Path | None = None) -> Path:
    events = read_events(root)
    path = summary_path(root)
    existing_summary = path.read_text(encoding="utf-8") if path.exists() else ""

    # L-037: Project purpose is structural, not event-derived. Pull it from
    # PROJECT_MAP.md (the user-/AI-authored project description) so any
    # update to PROJECT_MAP.md flows through to summary.md on the next
    # regen. Falls back to whatever summary.md had before (legacy repos),
    # then to the default placeholder.
    try:
        map_purpose = extract_project_purpose_from_map(project_map_path(root))
    except Exception:
        map_purpose = None
    project_purpose = map_purpose or extract_project_purpose(existing_summary)

    content = build_summary(events, root or Path.cwd(), project_purpose=project_purpose)
    path.write_text(content, encoding="utf-8")
    write_issue_files(events, root)
    return path


def extract_project_purpose_from_map(map_path: Path) -> str | None:
    """Read the `## Project purpose` section from PROJECT_MAP.md.

    Returns the body as a string if it's been populated with real content,
    or None if PROJECT_MAP.md is missing, the section is missing, or the
    body still looks like one of the known placeholder phrases.
    """
    if not map_path.exists():
        return None
    content = map_path.read_text(encoding="utf-8")
    match = re.search(
        r"^## Project purpose\s*\n(?P<body>.*?)(?=\n## |\Z)",
        content,
        flags=re.DOTALL | re.MULTILINE,
    )
    if not match:
        return None
    body = match.group("body").strip()
    if _looks_like_placeholder(body):
        return None
    return body


def build_summary(
    events: list[Event], root: Path, project_purpose: str | None = None
) -> str:
    project_name = root.name
    now = datetime.now(timezone.utc).date().isoformat()
    issues = group_issue_events(events)
    # Superseded decisions stay in the log (append-only audit trail) but
    # drop out of the live summary — only the current decision should steer
    # an AI session. Retired ones remain reachable via `pjm search`.
    retired = superseded_ids(events)
    decisions = [
        event
        for event in events
        if event.type == "decision" and event.id not in retired
    ]
    notes = [event for event in events if event.type == "note"]

    lines = [
        f"# projectmem - {project_name}",
        "",
        f"_Last updated: {now}_",
        "",
        "## Project purpose",
        project_purpose or (
            "Replace this placeholder with a concise description of what this "
            "project does, who it serves, and the main technologies or runtime "
            "assumptions."
        ),
        "",
        "## Recent issues",
    ]

    # An issue group can lack its `issue` event — an attempt recorded against
    # a mistyped id, or a hand-edited log. Skip it here rather than crash:
    # regeneration runs after every write, so one bad event used to make every
    # later command fail.
    groups = []
    for issue_id, issue_events in sorted(issues.items(), key=_issue_sort_key, reverse=True):
        issue = next((event for event in issue_events if event.type == "issue"), None)
        if issue is not None:
            groups.append((issue_id, issue, issue_events, _fix_for(issue_events)))
    if not groups:
        lines.append("- No issues logged yet.")
    else:
        # A scannable snapshot, not the whole backlog (#19). Every OPEN issue
        # stays — it is the one thing the next session must not miss. The
        # newest OPEN_IN_FULL are written out with their failed attempts; the
        # rest are named by id on one line (real projects reach 30–40 open
        # issues, and each full entry carries up to three attempt lines).
        # Fixed issues are capped. Everything stays in `pjm search`,
        # `get_issue(issue_id)` and `.projectmem/issues/`.
        open_groups = [g for g in groups if g[3] is None]
        fixed_groups = [g for g in groups if g[3] is not None]
        for group in open_groups[:OPEN_IN_FULL]:
            lines.extend(_issue_lines(*group))
        more_open = open_groups[OPEN_IN_FULL:]
        if more_open:
            ids = ", ".join(f"#{g[0]}" for g in more_open)
            lines.append(
                f"- [OPEN] {len(more_open)} older open issue{'s' if len(more_open) != 1 else ''}, "
                f"by id: {ids} (`get_issue(issue_id)` for details)"
            )
        for group in fixed_groups[:RECENT_FIXED_ISSUES]:
            lines.extend(_issue_lines(*group))
        hidden = len(fixed_groups) - RECENT_FIXED_ISSUES
        if hidden > 0:
            lines.append(
                f"- ...and {hidden} older fixed issue{'s' if hidden != 1 else ''} "
                "not shown (`pjm search`, or `get_issue(issue_id)` via MCP)"
            )

    lines.extend(["", "## Decisions"])
    if decisions:
        # Same bound for decisions (#19): on an aged project this section was
        # most of the summary. Newest kept, in the order they were made.
        hidden = len(decisions) - RECENT_DECISIONS
        if hidden > 0:
            lines.append(
                f"- ...{hidden} earlier decision{'s' if hidden != 1 else ''} "
                "not shown (`pjm search`, or `search_events(query)` via MCP)"
            )
        for event in decisions[-RECENT_DECISIONS:]:
            lines.append(f"- {_clip(event.summary)}{_loc(event.location)}")
    else:
        lines.append("- No decisions logged yet.")

    lines.extend(["", "## Notes"])
    if notes:
        for event in notes[-10:]:
            lines.append(f"- {_clip(event.summary)}{_loc(event.location)}")
    else:
        lines.append("- No notes logged yet.")

    lines.extend(["", "## Key files"])
    key_files = collect_files(events)
    if key_files:
        for file_path in key_files[:20]:
            lines.append(f"- `{file_path}`")
    else:
        lines.append("- No key files logged yet.")

    lines.extend(["", "## Open questions"])
    lines.append("- None logged yet.")
    lines.append("")
    return "\n".join(lines)


def extract_project_purpose(summary: str) -> str | None:
    """Pull the Project purpose section out of an existing summary.md.

    Returns None when missing or still placeholder, so the regenerator
    knows to fall back to PROJECT_MAP.md or the default template. L-037
    broadened the placeholder detection beyond the historical
    "Not described yet." check — without that, the `pjm init` placeholder
    ("Replace this placeholder...") was treated as real content and
    silently round-tripped forever, hiding the bug that motivated L-037.
    """
    match = re.search(
        r"^## (?:Project purpose|What this project is)\n(?P<body>.*?)(?=\n## |\Z)",
        summary,
        flags=re.DOTALL | re.MULTILINE,
    )
    if not match:
        return None
    body = match.group("body").strip()
    if _looks_like_placeholder(body):
        return None
    return body


def group_issue_events(events: list[Event]) -> dict[str, list[Event]]:
    issues: dict[str, list[Event]] = defaultdict(list)
    for event in events:
        if event.issue_id:
            issues[event.issue_id].append(event)
    return dict(issues)


def collect_files(events: list[Event]) -> list[str]:
    seen: set[str] = set()
    files: list[str] = []
    for event in events:
        for explicit in event.files:
            if explicit not in seen:
                seen.add(explicit)
                files.append(explicit)
        for inferred in infer_file_mentions(event.summary):
            if inferred not in seen:
                seen.add(inferred)
                files.append(inferred)
    return files


def infer_file_mentions(text: str) -> list[str]:
    pattern = r"(?<![\w/.-])[\w./-]+\.[A-Za-z0-9]+(?::\d+)?"
    return re.findall(pattern, text)


def write_issue_files(events: list[Event], root: Path | None = None) -> None:
    for issue_id, issue_events in group_issue_events(events).items():
        issue = next((event for event in issue_events if event.type == "issue"), None)
        if issue is None:
            continue
        slug = slugify(issue.summary)
        path = issues_dir(root) / f"{issue_id}-{slug}.md"
        lines = [f"# #{issue_id} {issue.summary}", ""]
        for event in issue_events:
            loc = f" [{event.location}]" if event.location else ""
            detail = f"- {event.timestamp} `{event.type}`: {event.summary}{loc}"
            if event.outcome:
                detail += f" ({event.outcome})"
            lines.append(detail)
        lines.append("")
        path.write_text("\n".join(lines), encoding="utf-8")


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug or "issue")[:48].strip("-") or "issue"
