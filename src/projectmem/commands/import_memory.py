"""`pjm import`: bring the useful part of another memory tool's history over.

Each source item either maps onto one of projectmem's own event types or is
dropped; a source's own concepts never become new types. Raw activity (file
reads and writes, commands, searches) is the bulk of most exports and is
dropped on purpose: imported memory has to be as useful as memory written
here, or it drowns the signal that makes projectmem worth reading.

The default run only shows what would be imported. `--apply` writes the
events through `append_event`, the path every `pjm` write takes, so
redaction applies, and rebuilds the summary once at the end. Each event
names its source record in `notes`, so importing the same file twice adds
nothing.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import typer

from projectmem.models import Event, normalize_timestamp
from projectmem.staleness import project_relative, root_posix_of
from projectmem.storage import ProjectMemError, append_event, read_events, require_mem_dir
from projectmem.summary import regenerate_summary

SOURCES = ("agentmemory", "mem0")
PREVIEW_PER_TYPE = 5

EXPORT_HELP = {
    "agentmemory": (
        "With agentmemory running, export it with:\n"
        "    curl -s http://localhost:3111/agentmemory/export > agentmemory-export.json"
    ),
    "mem0": (
        "Export your memories with mem0's own get_all, for example:\n"
        "    python -c \"import json; from mem0 import Memory; "
        "json.dump(Memory().get_all(filters={'user_id': 'YOU'}), "
        "open('mem0-export.json', 'w'), default=str)\""
    ),
}

# agentmemory memory types that are design choices; the rest are notes.
_AGENTMEMORY_DECISION_TYPES = {"architecture"}
_AGENTMEMORY_NOTE_TYPES = {"pattern", "preference", "bug", "workflow", "fact"}
# Hook payloads are stored as `{"file_path": …} | text`; keep the text.
_PAYLOAD_PREFIX = re.compile(r"^\s*\{.*?\}\s*\|\s*", re.S)


class ImportFormatError(Exception):
    pass


@dataclass
class Candidate:
    type: str            # decision | note | attempt
    summary: str
    source_id: str
    kind: str            # what it was in the source, for the preview
    timestamp: str | None = None
    files: list[str] = field(default_factory=list)
    outcome: str | None = None


@dataclass
class Plan:
    source: str
    candidates: list[Candidate] = field(default_factory=list)
    skipped: Counter = field(default_factory=Counter)
    other_projects: Counter = field(default_factory=Counter)
    matched: int = 0     # records that belong to this project, kept or not


def _text(value: object) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _files(value: object, root_posix: str) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if isinstance(item, str) and item.strip():
            rel = project_relative(item.strip(), root_posix=root_posix)
            if rel not in out:
                out.append(rel)
    return out


# ── agentmemory ──────────────────────────────────────────────────────────


def _project_matcher(root: Path, source_project: str | None):
    root_resolved = root.resolve()

    def matches(project: object) -> bool:
        if not isinstance(project, str) or not project.strip():
            return False
        project = project.strip()
        if source_project is not None:
            if project == source_project:
                return True
            try:
                return Path(project).resolve() == Path(source_project).expanduser().resolve()
            except OSError:
                return False
        candidate = Path(project)
        if not candidate.is_absolute():
            return project == root.name
        try:
            resolved = candidate.resolve()
        except OSError:
            return False
        return resolved == root_resolved or root_resolved in resolved.parents

    return matches


def plan_agentmemory(data: object, root: Path, source_project: str | None = None) -> Plan:
    if not isinstance(data, dict) or not ({"sessions", "memories"} & set(data)):
        raise ImportFormatError(
            "This does not look like an agentmemory export: expected a JSON object "
            "with 'sessions' and 'memories'."
        )
    plan = Plan("agentmemory")
    matches = _project_matcher(root, source_project)
    root_posix = root_posix_of(root)

    def keep(record: dict) -> bool:
        project = record.get("project")
        if matches(project):
            plan.matched += 1
            return True
        if isinstance(project, str) and project.strip():
            plan.other_projects[project.strip()] += 1
        else:
            plan.skipped["no project named"] += 1
        return False

    for memory in data.get("memories") or []:
        if not isinstance(memory, dict) or not keep(memory):
            continue
        if memory.get("isLatest") is False:
            plan.skipped["superseded in agentmemory"] += 1
            continue
        text = _text(memory.get("content")) or _text(memory.get("title"))
        kind = str(memory.get("type") or "fact")
        if not text:
            plan.skipped["empty"] += 1
            continue
        if kind in _AGENTMEMORY_DECISION_TYPES:
            event_type = "decision"
        elif kind in _AGENTMEMORY_NOTE_TYPES:
            event_type = "note"
        else:
            plan.skipped[f"memory type '{kind}'"] += 1
            continue
        plan.candidates.append(Candidate(
            event_type, text, str(memory.get("id") or ""), f"memory ({kind})",
            memory.get("createdAt"), _files(memory.get("files"), root_posix),
        ))

    for lesson in data.get("lessons") or []:
        if not isinstance(lesson, dict) or not keep(lesson):
            continue
        text = _text(lesson.get("content"))
        if not text:
            plan.skipped["empty"] += 1
            continue
        plan.candidates.append(Candidate(
            "note", text, str(lesson.get("id") or ""), "lesson", lesson.get("createdAt"),
        ))

    for crystal in data.get("crystals") or []:
        # A crystal's own lessons are also stored as lesson records, so only
        # its narrative is taken here.
        if not isinstance(crystal, dict) or not keep(crystal):
            continue
        text = _text(crystal.get("narrative"))
        if not text:
            plan.skipped["empty"] += 1
            continue
        plan.candidates.append(Candidate(
            "note", text, str(crystal.get("id") or ""), "crystal", crystal.get("createdAt"),
            _files(crystal.get("filesAffected"), root_posix),
        ))

    session_project = {
        s.get("id"): s.get("project") for s in data.get("sessions") or [] if isinstance(s, dict)
    }
    observations = data.get("observations") or {}
    rows = observations.values() if isinstance(observations, dict) else [observations]
    for group in rows:
        for obs in group if isinstance(group, list) else []:
            if not isinstance(obs, dict):
                continue
            if not keep({"project": obs.get("project") or session_project.get(obs.get("sessionId"))}):
                continue
            kind = obs.get("type")
            if kind not in ("error", "decision"):
                plan.skipped["raw activity (reads, writes, commands)"] += 1
                continue
            narrative = _PAYLOAD_PREFIX.sub("", obs.get("narrative") or "")
            text = _text(narrative) or _text(obs.get("title"))
            if not text:
                plan.skipped["empty"] += 1
                continue
            plan.candidates.append(Candidate(
                "attempt" if kind == "error" else "decision",
                text, str(obs.get("id") or ""), f"observation ({kind})",
                obs.get("timestamp"), _files(obs.get("files"), root_posix),
                outcome="failed" if kind == "error" else None,
            ))
    return plan


# ── mem0 ─────────────────────────────────────────────────────────────────


def plan_mem0(data: object, root: Path) -> Plan:
    items = data.get("results", data.get("memories")) if isinstance(data, dict) else data
    if not isinstance(items, list) or not all(
        isinstance(i, dict) and "memory" in i for i in items
    ):
        raise ImportFormatError(
            "This does not look like a mem0 export: expected the output of mem0's "
            "get_all, a list of records with a 'memory' field."
        )
    plan = Plan("mem0")
    for item in items:
        text = _text(item.get("memory"))
        if not text:
            plan.skipped["empty"] += 1
            continue
        # mem0 memories are untyped facts, so every one becomes a note.
        plan.candidates.append(Candidate(
            "note", text, str(item.get("id") or ""), "memory", item.get("created_at"),
        ))
    return plan


# ── shared: de-duplicate, preview, write ─────────────────────────────────


def _imported_marker(source: str, source_id: str) -> str:
    return f"Imported from {source}: {source_id}"


def drop_known(plan: Plan, existing: list[Event]) -> None:
    """Remove what this project already has: earlier imports and the same text."""
    imported = {e.notes for e in existing if (e.notes or "").startswith(f"Imported from {plan.source}")}
    known_text = {(e.type, e.summary.strip().lower()) for e in existing}
    kept, seen = [], set()
    for c in plan.candidates:
        key = (c.type, c.summary.strip().lower())
        if c.source_id and _imported_marker(plan.source, c.source_id) in imported:
            plan.skipped["already imported"] += 1
        elif key in known_text:
            plan.skipped["already in this project's memory"] += 1
        elif key in seen:
            plan.skipped["duplicate in the export"] += 1
        else:
            seen.add(key)
            kept.append(c)
    plan.candidates = kept


def write(plan: Plan, root: Path) -> int:
    # Build (and so validate) every event before writing any, so a bad
    # record cannot leave half an import behind.
    events = []
    for c in plan.candidates:
        event = Event(
            type=c.type,
            summary=c.summary,
            outcome=c.outcome,
            files=c.files,
            location=c.files[0] if c.files else None,
            # Provenance lives in notes, which every projectmem version reads;
            # a new capture_source value would make older versions reject the log.
            notes=_imported_marker(plan.source, c.source_id) if c.source_id else f"Imported from {plan.source}",
        )
        if c.timestamp:
            event.timestamp = normalize_timestamp(str(c.timestamp))
        events.append(event)
    for event in events:
        append_event(event, root)
    if events:
        regenerate_summary(root)
    return len(plan.candidates)


_LABELS = {"decision": "decision", "note": "note", "attempt": "failed attempt"}


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def print_plan(plan: Plan, root: Path, applied: bool) -> None:
    by_type = Counter(c.type for c in plan.candidates)
    parts = [_plural(by_type[t], _LABELS[t]) for t in ("decision", "note", "attempt") if by_type[t]]
    verb = "Imported" if applied else "Would import"
    typer.echo(f"{verb} from {plan.source} into '{root.name}': {', '.join(parts) or 'nothing'}")
    if plan.skipped:
        reasons = ", ".join(f"{n} {why}" for why, n in plan.skipped.most_common())
        typer.echo(f"  Skipped: {reasons}")
    if plan.other_projects:
        total = sum(plan.other_projects.values())
        typer.echo(f"  Left out: {total} record(s) from other projects:")
        for project, n in plan.other_projects.most_common(5):
            typer.echo(f"    {n:>4}  {project}")
        if not plan.matched:
            typer.echo(
                "  If one of those is this project under another path, run again "
                "with --source-project <that path>."
            )
    if applied or not plan.candidates:
        return
    typer.echo("")
    for event_type in ("decision", "note", "attempt"):
        rows = [c for c in plan.candidates if c.type == event_type]
        for c in rows[:PREVIEW_PER_TYPE]:
            line = c.summary if len(c.summary) <= 90 else c.summary[:87] + "..."
            typer.echo(f"  {_LABELS[event_type]:<15} {line}")
        if len(rows) > PREVIEW_PER_TYPE:
            typer.echo(f"  {'':<15} ... and {len(rows) - PREVIEW_PER_TYPE} more")
    typer.echo("")
    typer.echo(f"Nothing written yet. Run again with --apply to add these {len(plan.candidates)} events.")


def load(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ImportFormatError(f"{path} not found.") from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ImportFormatError(f"{path} is not a JSON file.") from error


def run(path: Path, source: str, apply: bool = False, source_project: str | None = None) -> None:
    source = source.strip().lower()
    if source not in SOURCES:
        typer.echo(f"Unknown source: {source}. Use one of: {', '.join(SOURCES)}.", err=True)
        raise typer.Exit(1)
    if source_project is not None and source != "agentmemory":
        typer.echo("--source-project applies to agentmemory only; mem0 has no projects.", err=True)
        raise typer.Exit(1)
    try:
        root = require_mem_dir(None).parent
    except ProjectMemError as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(1) from error
    try:
        data = load(path)
        plan = (
            plan_agentmemory(data, root, source_project) if source == "agentmemory"
            else plan_mem0(data, root)
        )
    except ImportFormatError as error:
        typer.echo(f"Error: {error}", err=True)
        typer.echo(EXPORT_HELP[source], err=True)
        raise typer.Exit(1) from error
    drop_known(plan, read_events(root))
    if apply:
        write(plan, root)
    print_plan(plan, root, applied=apply)
