from __future__ import annotations

import subprocess
from pathlib import Path

import typer

from projectmem.models import Event, normalize_timestamp
from projectmem.storage import append_event, read_events
from projectmem.summary import regenerate_summary

FIELD_SEP = "\x1f"  # what git prints for %x1f in the log format below


def run(limit: int = 20, root: Path | None = None) -> None:
    root_path = root or Path.cwd()
    
    # 1. Get git log
    try:
        result = subprocess.run(
            # Fields are separated by the unit separator (0x1f), which cannot
            # appear in a commit subject or author name the way "|" can.
            ["git", "log", f"-n {limit}", "--pretty=format:%h%x1f%s%x1f%an%x1f%ai"],
            cwd=root_path,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        typer.echo("Error: Could not read git log. Are you in a git repository?", err=True)
        return

    lines = result.stdout.splitlines()
    if not lines:
        typer.echo("No git history found.")
        return

    # 2. Parse and create events
    existing_events = read_events(root)
    existing_commits = {e.git_commit for e in existing_events if e.git_commit}
    
    new_count = 0
    for line in reversed(lines): # Start from oldest
        parts = line.split(FIELD_SEP)
        if len(parts) != 4:
            continue
            
        commit_hash, message, author, date = parts
        
        if commit_hash in existing_commits:
            continue
            
        # Infer type
        msg_lower = message.lower()
        event_type = "note"
        outcome = None
        issue_id = None
        
        if any(kw in msg_lower for kw in ["fix", "resolve", "close", "bug", "issue"]):
            event_type = "fix"
            # Create a pseudo-issue for this fix
            issue_id = f"legacy_{commit_hash[:4]}"
            issue_event = Event(
                type="issue",
                issue_id=issue_id,
                summary=f"Legacy issue: {message}",
                timestamp=normalize_timestamp(date),
                git_commit=commit_hash,
                notes="Created during backfill"
            )
            append_event(issue_event, root)
        elif "revert" in msg_lower:
            event_type = "attempt"
            outcome = "failed"
        
        # Get files
        files = get_commit_files(commit_hash, root_path)
        
        event = Event(
            type=event_type,
            issue_id=issue_id,
            summary=message,
            timestamp=normalize_timestamp(date),
            outcome=outcome,
            files=files,
            git_commit=commit_hash,
            notes=f"Auto-backfilled from git commit by {author}"
        )
        
        append_event(event, root)
        new_count += 1

    if new_count > 0:
        regenerate_summary(root)
        typer.echo(f"Backfilled {new_count} events from git history.")
    else:
        typer.echo("No new events to backfill.")


def get_commit_files(commit_hash: str, root: Path) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "show", "--name-only", "--pretty=format:", commit_hash],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        return [f.strip() for f in result.stdout.splitlines() if f.strip()]
    except (OSError, subprocess.CalledProcessError):
        return []
