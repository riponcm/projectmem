"""CLI command for managing cross-project global memory.

Usage:
    pjm global status                   # overview of global memory
    pjm global list                     # list all patterns + gotchas
    pjm global list --tag python        # filter by tag
    pjm global list --library sqlalchemy # filter by library
    pjm global add "pattern text"       # add a pattern
    pjm global add-gotcha sqlalchemy "always close async sessions"
    pjm global remove <id>              # remove by ID
    pjm global export                   # export as JSON
    pjm global import file.json         # import from JSON
    pjm global prune --older-than 365   # remove stale entries
    pjm global detect                   # detect stack in current project
"""
from __future__ import annotations

import json
from pathlib import Path

import typer

from projectmem.global_memory import (
    add_gotcha,
    add_pattern,
    auto_promote_event,
    detect_stack,
    export_all,
    get_relevant_entries,
    global_dir,
    import_all,
    preferences_path,
    prune_entries,
    read_gotchas,
    read_patterns,
    remove_entry,
)


def run(
    action: str = "status",
    text: str | None = None,
    tag: str | None = None,
    library: str | None = None,
    file: str | None = None,
    older_than: int = 365,
    confidence: str | None = None,
    fmt: str = "text",
    root: Path | None = None,
) -> None:
    """Dispatch global memory actions."""
    # L-026b: `pjm global add "..." --library X` is the natural-feeling form
    # for adding a library gotcha. Auto-route it to add-gotcha rather than
    # erroring on the extra positional. Pattern adds explicitly omit --library.
    if action == "add" and library:
        action = "add-gotcha"

    if action == "status":
        _status()
    elif action == "list":
        _list_entries(tag=tag, library=library, fmt=fmt)
    elif action == "add":
        _add_pattern(text, tag=tag, root=root)
    elif action == "add-gotcha":
        _add_gotcha(library=library, text=text, tag=tag, root=root)
    elif action == "remove":
        _remove(text)
    elif action == "export":
        _export()
    elif action == "import":
        _import(file)
    elif action == "prune":
        _prune(older_than=older_than, confidence=confidence)
    elif action == "detect":
        _detect(root, fmt=fmt)
    else:
        typer.echo(
            f"Unknown action: {action}\n"
            "Available: status, list, add, add-gotcha, remove, export, import, prune, detect"
        )


def _status() -> None:
    """Show global memory overview."""
    bold = "\033[1m"
    dim = "\033[2m"
    cyan = "\033[36m"
    green = "\033[32m"
    reset = "\033[0m"

    patterns = read_patterns()
    gotchas = read_gotchas()
    prefs_exist = preferences_path().exists()

    # Collect unique tags and libraries
    all_tags: set[str] = set()
    all_libs: set[str] = set()
    all_projects: set[str] = set()

    for p in patterns:
        all_tags.update(p.get("tags", []))
        proj = p.get("source_project")
        if proj:
            all_projects.add(proj)

    for g in gotchas:
        all_tags.update(g.get("tags", []))
        all_libs.add(g.get("library", ""))
        proj = g.get("source_project")
        if proj:
            all_projects.add(proj)

    all_libs.discard("")

    bar = "=" * 48
    typer.echo(f"\n  {dim}{bar}{reset}")
    typer.echo(f"  {bold}  projectmem Global Memory{reset}")
    typer.echo(f"  {dim}{bar}{reset}")
    typer.echo(f"  {dim}Location: {global_dir()}{reset}")
    typer.echo(f"")
    typer.echo(f"    Patterns:           {bold}{len(patterns)}{reset}")
    typer.echo(f"    Library gotchas:     {bold}{len(gotchas)}{reset}")
    typer.echo(f"    Stack preferences:   {bold}{'yes' if prefs_exist else 'not set'}{reset}")
    typer.echo(f"    Unique tags:         {bold}{len(all_tags)}{reset}")
    typer.echo(f"    Libraries tracked:   {bold}{len(all_libs)}{reset}")
    typer.echo(f"    Source projects:     {bold}{len(all_projects)}{reset}")

    if all_tags:
        typer.echo(f"\n  {dim}Tags:{reset} {', '.join(sorted(all_tags))}")
    if all_libs:
        typer.echo(f"  {dim}Libraries:{reset} {', '.join(sorted(all_libs))}")
    if all_projects:
        typer.echo(f"  {dim}Projects:{reset} {', '.join(sorted(all_projects))}")

    typer.echo(f"  {dim}{bar}{reset}\n")


def _list_entries(
    tag: str | None = None, library: str | None = None, fmt: str = "text"
) -> None:
    """List patterns and gotchas, optionally filtered."""
    bold = "\033[1m"
    dim = "\033[2m"
    cyan = "\033[36m"
    yellow = "\033[33m"
    reset = "\033[0m"

    patterns = read_patterns()
    gotchas = read_gotchas()

    # Filter
    if tag:
        patterns = [p for p in patterns if tag.lower() in [t.lower() for t in p.get("tags", [])]]
        gotchas = [g for g in gotchas if tag.lower() in [t.lower() for t in g.get("tags", [])]]
    if library:
        gotchas = [g for g in gotchas if library.lower() in g.get("library", "").lower()]

    if fmt == "json":
        typer.echo(json.dumps({"patterns": patterns, "gotchas": gotchas}, indent=2))
        return

    if not patterns and not gotchas:
        typer.echo("No entries found." + (f" (filter: tag={tag}, library={library})" if tag or library else ""))
        typer.echo("  Add patterns:  pjm global add \"your pattern here\"")
        typer.echo("  Add gotchas:   pjm global add-gotcha \"gotcha text\" --library <library>")
        return

    if patterns:
        typer.echo(f"\n{bold}Patterns ({len(patterns)}){reset}")
        typer.echo(f"{dim}{'─' * 60}{reset}")
        for p in patterns:
            pid = p.get("id", "?")
            text = p.get("pattern", "")
            tags = ", ".join(p.get("tags", []))
            source = p.get("source_project", "")
            conf = p.get("confidence", "")
            typer.echo(f"  {cyan}{pid}{reset}  {text}")
            meta_parts = []
            if tags:
                meta_parts.append(f"tags: {tags}")
            if source:
                meta_parts.append(f"from: {source}")
            if conf:
                meta_parts.append(f"confidence: {conf}")
            if meta_parts:
                typer.echo(f"    {dim}{' · '.join(meta_parts)}{reset}")

    if gotchas:
        typer.echo(f"\n{bold}Library Gotchas ({len(gotchas)}){reset}")
        typer.echo(f"{dim}{'─' * 60}{reset}")
        for g in gotchas:
            gid = g.get("id", "?")
            lib = g.get("library", "unknown")
            text = g.get("gotcha", "")
            source = g.get("source_project", "")
            version = g.get("version_range", "")
            typer.echo(f"  {yellow}{gid}{reset}  [{bold}{lib}{reset}] {text}")
            meta_parts = []
            if version:
                meta_parts.append(f"version: {version}")
            if source:
                meta_parts.append(f"from: {source}")
            if meta_parts:
                typer.echo(f"    {dim}{' · '.join(meta_parts)}{reset}")

    typer.echo("")


def _add_pattern(text: str | None, tag: str | None = None, root: Path | None = None) -> None:
    """Add a pattern to global memory."""
    if not text:
        typer.echo("Usage: pjm global add \"your pattern here\" [--tag python]")
        return

    tags = [tag] if tag else []
    project = (root or Path.cwd()).name

    entry = add_pattern(pattern=text, tags=tags, source_project=project)
    typer.echo(f"\033[32m[global]\033[0m Pattern added: {entry['id']}")
    typer.echo(f"  {text}")


def _add_gotcha(
    library: str | None, text: str | None, tag: str | None = None, root: Path | None = None
) -> None:
    """Add a library gotcha to global memory."""
    if not library or not text:
        typer.echo('Usage: pjm global add-gotcha --library sqlalchemy "gotcha text" [--tag python]')
        return

    tags = [tag] if tag else []
    project = (root or Path.cwd()).name

    entry = add_gotcha(library=library, gotcha=text, tags=tags, source_project=project)
    typer.echo(f"\033[32m[global]\033[0m Gotcha added: {entry['id']}")
    typer.echo(f"  [{library}] {text}")


def _remove(entry_id: str | None) -> None:
    """Remove an entry by ID."""
    if not entry_id:
        typer.echo("Usage: pjm global remove <id>")
        return

    if remove_entry(entry_id):
        typer.echo(f"\033[32m[global]\033[0m Removed: {entry_id}")
    else:
        typer.echo(f"Entry not found: {entry_id}")


def _export() -> None:
    """Export global memory as JSON."""
    data = export_all()
    typer.echo(json.dumps(data, indent=2))


def _import(file_path: str | None) -> None:
    """Import global memory from JSON file."""
    if not file_path:
        typer.echo("Usage: pjm global import <file.json>")
        return

    path = Path(file_path)
    if not path.exists():
        typer.echo(f"File not found: {file_path}")
        return

    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        typer.echo(f"Invalid JSON: {e}")
        return

    counts = import_all(data, merge=True)
    typer.echo(
        f"\033[32m[global]\033[0m Imported: "
        f"{counts['patterns']} patterns, {counts['gotchas']} gotchas"
    )


def _prune(older_than: int = 365, confidence: str | None = None) -> None:
    """Remove old entries."""
    removed = prune_entries(older_than_days=older_than, confidence=confidence)
    if removed:
        typer.echo(f"\033[32m[global]\033[0m Pruned {removed} entries (older than {older_than} days)")
    else:
        typer.echo("No entries to prune.")


def _detect(root: Path | None = None, fmt: str = "text") -> None:
    """Detect and display the current project's stack."""
    root_path = root or Path.cwd()
    stack = detect_stack(root_path)

    if fmt == "json":
        typer.echo(json.dumps({"name": root_path.name, **stack}, indent=2))
        return

    bold = "\033[1m"
    dim = "\033[2m"
    cyan = "\033[36m"
    reset = "\033[0m"

    typer.echo(f"\n{bold}Detected Stack: {root_path.name}{reset}")
    typer.echo(f"{dim}{'─' * 50}{reset}")

    if stack["tags"]:
        typer.echo(f"  Tags:       {cyan}{', '.join(stack['tags'])}{reset}")
    else:
        typer.echo(f"  Tags:       {dim}(none detected){reset}")

    if stack["libraries"]:
        typer.echo(f"  Libraries:  {', '.join(stack['libraries'][:15])}")
        if len(stack["libraries"]) > 15:
            typer.echo(f"              ... and {len(stack['libraries']) - 15} more")
    else:
        typer.echo(f"  Libraries:  {dim}(none detected){reset}")

    if stack["frameworks"]:
        typer.echo(f"  Frameworks: {cyan}{', '.join(stack['frameworks'])}{reset}")

    typer.echo(f"  Manifests:  {', '.join(stack['manifest_files'])}")

    # Show what would be inherited
    relevant = get_relevant_entries(stack)
    r_patterns = relevant["patterns"]
    r_gotchas = relevant["gotchas"]

    if r_patterns or r_gotchas:
        typer.echo(f"\n  {bold}Would inherit:{reset}")
        typer.echo(f"    {len(r_patterns)} patterns, {len(r_gotchas)} library gotchas")
    else:
        typer.echo(f"\n  {dim}No matching global entries for this stack.{reset}")

    typer.echo("")
