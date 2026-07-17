from __future__ import annotations

from pathlib import Path

import typer

from projectmem.commands import attempt as attempt_command
from projectmem.commands import auto_capture as auto_capture_command
from projectmem.commands import backfill as backfill_command
from projectmem.commands import brief as brief_command
from projectmem.commands import context as context_command
from projectmem.commands import decision as decision_command
from projectmem.commands import export as export_command
from projectmem.commands import global_cmd as global_command
from projectmem.commands import fix as fix_command
from projectmem.commands import hooks as hooks_command
from projectmem.commands import init as init_command
from projectmem.commands import instructions as instructions_command
from projectmem.commands import log as log_command
from projectmem.commands import map as map_command
from projectmem.commands import note as note_command
from projectmem.commands import precheck as precheck_command
from projectmem.commands import project as project_command
from projectmem.commands import regenerate as regenerate_command
from projectmem.commands import score as score_command
from projectmem.commands import search as search_command
from projectmem.commands import show as show_command
from projectmem.commands import stats as stats_command
from projectmem.commands import visualize as visualize_command
from projectmem.commands import watch as watch_command
from projectmem.commands import wrap as wrap_command
from projectmem.storage import ProjectMemError

app = typer.Typer(
    help="Project memory for humans and AI tools.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(project_command.project_app, name="project")


@app.callback()
def callback() -> None:
    """Capture issues, attempts, fixes, decisions, and notes for this repo."""


@app.command()
def init(
    no_hooks: bool = typer.Option(False, "--no-hooks", help="Skip git hook installation."),
    no_global: bool = typer.Option(False, "--no-global", help="Skip global memory inheritance."),
    no_watch: bool = typer.Option(False, "--no-watch", help="Skip auto-starting the file watcher."),
    no_backfill: bool = typer.Option(False, "--no-backfill", help="Skip ingesting recent git history."),
    no_claude_md: bool = typer.Option(False, "--no-claude-md", help="Skip writing/updating the CLAUDE.md bridge block."),
    no_stack_detect: bool = typer.Option(False, "--no-stack-detect", help="Skip auto-populating PROJECT_MAP.md from detected stack."),
    no_mcp_config: bool = typer.Option(False, "--no-mcp-config", help="Skip printing the MCP client config block at the end."),
    global_tags: str | None = typer.Option(None, "--global-tags", help="Only inherit matching tags (comma-separated)."),
) -> None:
    """Create .projectmem/ in the current repo."""
    init_command.run(
        no_hooks=no_hooks, no_global=no_global, no_watch=no_watch,
        no_backfill=no_backfill, no_claude_md=no_claude_md,
        no_stack_detect=no_stack_detect, no_mcp_config=no_mcp_config,
        global_tags=global_tags,
    )


@app.command()
def instructions() -> None:
    """Print AI instructions for this project."""
    instructions_command.run()


@app.command("map")
def project_map() -> None:
    """Print the project map."""
    map_command.run()


@app.command()
def log(
    text: str,
    at: str | None = typer.Option(None, "--at", help="Location (e.g. file:line, class.method)"),
) -> None:
    """Start a new issue."""
    log_command.run(text, location=at)


@app.command()
def attempt(
    text: str,
    worked: bool = typer.Option(False, "--worked", help="Mark the attempt as worked."),
    failed: bool = typer.Option(False, "--failed", help="Mark the attempt as failed."),
    partial: bool = typer.Option(False, "--partial", help="Mark the attempt as partial."),
    at: str | None = typer.Option(None, "--at", help="Location (e.g. file:line, class.method)"),
    issue: str | None = typer.Option(None, "--issue", help="Attach to a specific issue ID (e.g. 0042)."),
    auto_issue: bool = typer.Option(
        False, "--auto-issue",
        help="If no active issue exists, auto-create one from this attempt's text.",
    ),
) -> None:
    """Record an attempt on the current issue."""
    attempt_command.run(
        text, worked=worked, failed=failed, partial=partial,
        location=at, issue=issue, auto_issue=auto_issue,
    )


@app.command()
def fix(
    text: str,
    at: str | None = typer.Option(
        None,
        "--at",
        help="Location (e.g. file:line, class.method)",
    ),
    issue: str | None = typer.Option(
        None,
        "--issue",
        help="Close a specific issue ID instead of the active issue (e.g. 0042).",
    ),
) -> None:
    """Record a fix and close the active issue, or a specific issue with --issue."""
    fix_command.run(text, location=at, issue=issue)


@app.command()
def decision(
    text: str,
    at: str | None = typer.Option(None, "--at", help="Location (e.g. file:line, class.method)"),
    supersedes: str | None = typer.Option(
        None, "--supersedes",
        help="Event id (or unique prefix) of a prior decision this one retires. "
             "The old event stays in the log, tagged (superseded); only the new "
             "one appears in summary.md.",
    ),
) -> None:
    """Record a decision (optionally retiring a prior one)."""
    try:
        decision_command.run(text, location=at, supersedes=supersedes)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command()
def note(
    text: str,
    at: str | None = typer.Option(None, "--at", help="Location (e.g. file:line, class.method)"),
) -> None:
    """Record a free-form note."""
    note_command.run(text, location=at)


@app.command()
def show() -> None:
    """Print the current summary.md."""
    show_command.run()


@app.command()
def search(
    query: str,
    regex: bool = typer.Option(
        False, "--regex", "-r",
        help="Treat query as a case-insensitive Python regex (enables OR-patterns).",
    ),
    failed_only: bool = typer.Option(
        False, "--failed-only",
        help="Only show failed attempts — the project's catalogue of dead ends.",
    ),
) -> None:
    """Substring (default) or regex search across events.

    Default mode is plain substring match. Add --regex for patterns like
    'carousel|favicon' — without it those are treated as literal text.
    """
    search_command.run(query, regex=regex, failed_only=failed_only)


@app.command()
def regenerate() -> None:
    """Rebuild summary.md from events.jsonl."""
    regenerate_command.run()


@app.command()
def brief() -> None:
    """One-screen session-start briefing: warnings, stale memories, open
    issues, recent decisions, stack gotchas, and the prevention score."""
    brief_command.run()


@app.command()
def export(
    claude_md: bool = typer.Option(
        True, "--claude-md/--no-claude-md",
        help="Write the memory block into CLAUDE.md (default).",
    ),
    cursor: bool = typer.Option(
        False, "--cursor", help="Also write the block into .cursorrules.",
    ),
    stdout: bool = typer.Option(
        False, "--stdout", help="Print the block instead of writing files.",
    ),
) -> None:
    """Compile live memory (decisions, gotchas, failed approaches) into
    CLAUDE.md so agents without MCP inherit the project's judgment."""
    export_command.run(claude_md=claude_md, cursor=cursor, stdout=stdout)


@app.command("visualize")
def visualize(
    output: Path | None = typer.Option(
        None, "--output", "-o",
        help="Where to write the HTML file. Default: .projectmem/viz.html",
    ),
    open_browser: bool = typer.Option(
        True, "--open/--no-open",
        help="Auto-open in the default browser (use --no-open in CI / headless).",
    ),
) -> None:
    """Generate an interactive visualization of project memory."""
    visualize_command.run(output=output, open_browser=open_browser)


@app.command()
def backfill(limit: int = typer.Option(20, help="Number of commits to scan.")) -> None:
    """Auto-populate memory from git history."""
    backfill_command.run(limit=limit)


@app.command()
def hooks(action: str = typer.Argument("install", help="Action: install or uninstall.")) -> None:
    """Manage projectmem git hooks (install/uninstall)."""
    hooks_command.run(action=action)


@app.command("_auto-capture", hidden=True)
def auto_capture(
    trigger: str = typer.Argument("commit", help="Trigger type: commit, merge."),
) -> None:
    """Internal: called by git hooks to auto-capture events."""
    auto_capture_command.run(trigger=trigger)


@app.command()
def stats(
    fmt: str = typer.Option("text", "--format", "-f", help="Output format: text, json."),
) -> None:
    """Show AI token savings and ROI."""
    stats_command.run(fmt=fmt)


@app.command()
def score(
    fmt: str = typer.Option("text", "--format", "-f", help="Output format: text, json, badge."),
    since: str | None = typer.Option(None, "--since", help="Time window (e.g. 30d, 4w, 2m)."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show detailed breakdown."),
) -> None:
    """Show failure prevention score and ROI metrics."""
    score_command.run(fmt=fmt, since=since, verbose=verbose)


@app.command()
def context(
    tokens: int = typer.Option(2000, "--tokens", "-t", help="Token budget."),
    focus: str | None = typer.Option(None, "--focus", help="Focus on file or directory."),
    recent: str | None = typer.Option(None, "--recent", help="Time window (e.g. 3d, 2w)."),
    fmt: str = typer.Option("md", "--format", "-f", help="Output format: md, json."),
) -> None:
    """Generate token-budgeted project context for AI agents."""
    context_command.run(tokens=tokens, focus=focus, recent=recent, fmt=fmt)


@app.command()
def precheck(
    files: list[str] = typer.Argument(
        None,
        help="Specific files to check (e.g. `pjm precheck payment.py auth.py`). "
             "Default: staged files.",
    ),
    level: str = typer.Option("warn", "--level", "-l", help="Strictness: info, warn, block."),
    working: bool = typer.Option(False, "--working", help="Check working tree instead of staged."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Only output if warnings present."),
    snooze: str | None = typer.Option(
        None, "--snooze",
        help="Silence precheck warnings for a duration (30m, 2h, 1d). "
             "The snooze is logged to memory, so the silence is audited.",
    ),
    unsnooze: bool = typer.Option(
        False, "--unsnooze", help="Re-enable warnings before the snooze expires.",
    ),
) -> None:
    """Check staged changes (or named files) against project memory."""
    precheck_command.run(
        level=level, working=working, quiet=quiet,
        files=files or None, snooze=snooze, unsnooze=unsnooze,
    )


@app.command()
def watch(
    daemon: bool = typer.Option(False, "--daemon", help="Run in background as a daemon."),
    stop: bool = typer.Option(False, "--stop", help="Stop the running watcher."),
    status: bool = typer.Option(False, "--status", help="Show watcher status."),
) -> None:
    """Watch file activity in real-time and log churn events (opt-in)."""
    watch_command.run(daemon=daemon, stop=stop, status=status)


@app.command()
def wrap(
    agent: str = typer.Argument("claude", help="Agent to wrap: claude, cursor, aider, generic."),
    tokens: int = typer.Option(2000, "--tokens", "-t", help="Context token budget."),
    focus: str | None = typer.Option(None, "--focus", help="Focus on file or directory."),
    recent: str | None = typer.Option(None, "--recent", help="Time window (e.g. 3d, 2w)."),
    preview: bool = typer.Option(False, "--preview", help="Show context without injecting."),
    no_warnings: bool = typer.Option(False, "--no-warnings", help="Skip failure warnings."),
) -> None:
    """Wrap an AI agent with auto-injected project memory."""
    wrap_command.run(
        agent=agent, tokens=tokens, focus=focus, recent=recent,
        preview=preview, no_warnings=no_warnings,
    )


@app.command("global")
def global_memory(
    action: str = typer.Argument("status", help="Action: status, list, add, add-gotcha, remove, export, import, prune, detect."),
    text: str | None = typer.Argument(None, help="Pattern text, gotcha text, entry ID, or import file path."),
    tag: str | None = typer.Option(None, "--tag", help="Filter or tag entries."),
    library: str | None = typer.Option(None, "--library", help="Filter by or specify library."),
    older_than: int = typer.Option(365, "--older-than", help="Days threshold for prune."),
    confidence: str | None = typer.Option(None, "--confidence", help="Confidence filter for prune."),
    fmt: str = typer.Option("text", "--format", "-f", help="Output format for list/detect: text, json."),
) -> None:
    """Manage cross-project global memory."""
    # For import, text is the file path
    file_path = text if action == "import" else None
    global_command.run(
        action=action, text=text, tag=tag, library=library,
        file=file_path, older_than=older_than, confidence=confidence, fmt=fmt,
    )


def main() -> None:
    try:
        app()
    except ProjectMemError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


if __name__ == "__main__":
    main()
