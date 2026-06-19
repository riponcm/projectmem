from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from projectmem.models import Event

MEM_DIR = ".projectmem"
SUMMARY_FILE = "summary.md"
EVENTS_FILE = "events.jsonl"
CONFIG_FILE = "config.toml"
ISSUES_DIR = "issues"
AI_INSTRUCTIONS_FILE = "AI_INSTRUCTIONS.md"
PROJECT_MAP_FILE = "PROJECT_MAP.md"


class ProjectMemError(RuntimeError):
    pass


def mem_path(root: Path | None = None) -> Path:
    return (root or Path.cwd()) / MEM_DIR


def _is_project_mem_dir(candidate: Path) -> bool:
    """True if `candidate` is a real, initialized project memory dir.

    `pjm init` always writes config.toml; the machine-wide global store at
    `~/.projectmem/` never has one. Without this check, walk-up discovery
    from any directory under $HOME that lacks its own project would land on
    the global store and misread it as project memory (0.1.4 fix — writes
    were silently accreting into `~/.projectmem/events.jsonl`).
    """
    return (
        candidate.is_dir()
        and (candidate / CONFIG_FILE).exists()
    )


def discover_mem_dir(start: Path | None = None) -> Path | None:
    """Walk up from `start` looking for `.projectmem/`, like git does for `.git/`.

    Returns the discovered .projectmem path, or None if none found in any
    parent. Only initialized project dirs count — see _is_project_mem_dir.
    """
    cur = (start or Path.cwd()).resolve()
    for path in [cur, *cur.parents]:
        candidate = path / MEM_DIR
        if _is_project_mem_dir(candidate):
            return candidate
    return None


def require_mem_dir(root: Path | None = None) -> Path:
    # If an explicit root was given, honor only that root (back-compat).
    if root is not None:
        path = mem_path(root)
        if path.exists():
            return path
        raise ProjectMemError(
            f"No .projectmem directory found in {root}. Run `projectmem init`."
        )

    # No explicit root: try CWD first, then walk up the directory tree.
    # The CWD candidate gets the same initialized-project validation as the
    # walk-up — running pjm from $HOME must not mistake the global store
    # (`~/.projectmem/`, no config.toml) for a project.
    cwd_path = mem_path(None)
    if _is_project_mem_dir(cwd_path):
        return cwd_path
    found = discover_mem_dir(None)
    if found is not None:
        return found
    raise ProjectMemError(
        f"No .projectmem directory found in {Path.cwd()} or any parent. "
        f"If running over MCP, set the project root via the `cwd` field in your "
        f"MCP client config or via the PROJECTMEM_ROOT environment variable. "
        f"Otherwise run `projectmem init` to create one."
    )


def events_path(root: Path | None = None) -> Path:
    return require_mem_dir(root) / EVENTS_FILE


def summary_path(root: Path | None = None) -> Path:
    return require_mem_dir(root) / SUMMARY_FILE


def ai_instructions_path(root: Path | None = None) -> Path:
    return require_mem_dir(root) / AI_INSTRUCTIONS_FILE


def project_map_path(root: Path | None = None) -> Path:
    return require_mem_dir(root) / PROJECT_MAP_FILE


def issues_dir(root: Path | None = None) -> Path:
    return require_mem_dir(root) / ISSUES_DIR


def initialize(root: Path | None = None) -> Path:
    root_path = root or Path.cwd()
    project_dir = mem_path(root_path)
    project_dir.mkdir(exist_ok=True)
    (project_dir / ISSUES_DIR).mkdir(exist_ok=True)
    (project_dir / EVENTS_FILE).touch(exist_ok=True)

    config = project_dir / CONFIG_FILE
    if not config.exists():
        config.write_text(
            'summary_size_limit_kb = 20\nrecent_days = 30\nproject_description = ""\n',
            encoding="utf-8",
        )

    summary = project_dir / SUMMARY_FILE
    if not summary.exists():
        summary.write_text(initial_summary(root_path), encoding="utf-8")

    instructions = project_dir / AI_INSTRUCTIONS_FILE
    if not instructions.exists():
        instructions.write_text(ai_instructions(), encoding="utf-8")

    project_map = project_dir / PROJECT_MAP_FILE
    if not project_map.exists():
        project_map.write_text(initial_project_map(root_path), encoding="utf-8")

    ensure_gitignore_entry(root_path)
    return project_dir


def initial_summary(root: Path) -> str:
    project_name = root.name
    return (
        f"# projectmem - {project_name}\n\n"
        "_Last updated: never (placeholder — populate via `pjm decision` / "
        "`pjm note` or the `add_decision` / `add_note` MCP tools)._\n\n"
        "## Project purpose\n"
        "Replace this placeholder with a concise description of what this project "
        "does, who it serves, and the main technologies or runtime assumptions.\n\n"
        "## How to use this memory\n"
        "AI assistants and human contributors should read this file before making "
        "changes. Keep it focused on durable context: current issues, decisions, "
        "failed attempts, gotchas, and files that matter.\n\n"
        "**For AI assistants finding this placeholder:** you are in Setup Mode. "
        "Read README, package metadata, and obvious entry points, then call "
        "`add_decision` and `add_note` (MCP) — or `pjm decision` / `pjm note` "
        "(CLI) — to record what you learned. Each call appends an event and "
        "auto-regenerates this summary. **Do NOT edit this file directly** — "
        "it is derived from `events.jsonl`. See `.projectmem/AI_INSTRUCTIONS.md` "
        "for the full Setup Mode workflow.\n\n"
        "## Current issues\n"
        "- None logged yet.\n\n"
        "## Recent fixes\n"
        "- None logged yet.\n\n"
        "## Decisions\n"
        "- None logged yet.\n\n"
        "## Known gotchas\n"
        "- None logged yet.\n\n"
        "## Key files\n"
        "- None logged yet.\n\n"
        "## Open questions\n"
        "- None logged yet.\n"
    )


def ai_instructions() -> str:
    return (
        "# projectmem AI Instructions\n\n"
        "These instructions are MANDATORY for all AI coding agents working in this "
        "project. Failure to follow them means your work is incomplete and the audit "
        "trail is corrupted.\n\n"
        "This file is stable operating guidance. Do not rewrite it unless the user "
        "asks or projectmem itself changes.\n\n"
        "## Start of every session\n\n"
        "**Step 1 — Identify your mode by reading `.projectmem/summary.md` and "
        "`.projectmem/PROJECT_MAP.md`.**\n\n"
        "- **Setup Mode** — `summary.md` and/or `PROJECT_MAP.md` still contain the "
        "**placeholder text** from `pjm init`. Concrete signals you are in Setup Mode:\n"
        "  - `summary.md` contains the phrase *\"Replace this placeholder with a "
        "concise description...\"*\n"
        "  - Section bodies say *\"None logged yet.\"*\n"
        "  - `PROJECT_MAP.md` contains *\"Status: not created yet\"* or *\"This file "
        "should be created by the first AI assistant...\"*\n\n"
        "  → **You MUST populate both files with real project content before doing "
        "any other work for the user.** This is not optional and not deferred — your "
        "first response in a Setup Mode session is the memory-population pass. "
        "Procedure:\n\n"
        "  1. Read `README.md`, `package.json` / `pyproject.toml` / `Cargo.toml`, "
        "entry-point files (typically `src/main.*`, `index.html`, "
        "`app/__init__.py`, etc.), and any obvious architectural files.\n"
        "  2. For **each architectural choice** you identify (frameworks, language, "
        "build system, deployment target, data flow): call `add_decision` (MCP) or "
        "`pjm decision` (CLI) — one call per decision.\n"
        "  3. For **each gotcha / setup detail / library quirk**: call `add_note` "
        "(MCP) or `pjm note` (CLI) — one call per gotcha.\n"
        "  4. Each `add_decision` / `add_note` call appends to `events.jsonl` AND "
        "auto-regenerates `summary.md`. **NEVER edit `summary.md` directly** — it is "
        "derived; your edits will be overwritten on the next event.\n"
        "  5. **DO edit `PROJECT_MAP.md` directly** to replace its placeholder. "
        "`PROJECT_MAP.md` is structural and is NOT derived from events. Write the "
        "project's purpose, main folders, entry points, important files, "
        "relationships, and suggested first reads. Make sure PROJECT_MAP.md has a "
        "`## Project purpose` section with a real description — that section is "
        "auto-copied into `summary.md`'s Project purpose on the next regeneration "
        "(the only path by which summary.md's Project purpose gets populated; "
        "there is intentionally no MCP tool for it).\n"
        "  6. After step 5, summary.md and PROJECT_MAP.md both contain real content "
        "(summary.md picks up the Project purpose from PROJECT_MAP.md on the next "
        "`add_decision` / `add_note` call's auto-regen — or you can force it now "
        "with `pjm regenerate`). The project is in Maintenance Mode for every "
        "subsequent session.\n\n"
        "- **Maintenance Mode** — `summary.md` AND `PROJECT_MAP.md` contain **real "
        "project content, NOT the `pjm init` placeholder text**. Concrete signals "
        "you are in Maintenance Mode:\n"
        "  - `summary.md` describes the actual project, lists real issues / "
        "decisions / notes by content.\n"
        "  - `PROJECT_MAP.md` has real folder descriptions, entry points, and file "
        "relationships — not *\"Status: not created yet.\"*\n\n"
        "  → **STOP analyzing the project structure.** The memory is already built. "
        "Use the existing summary + map. Focus exclusively on the user's actual task "
        "and on logging your own work via the trigger table.\n"
        "  - Do NOT re-scan source files. Trust the memory.\n"
        "  - Do NOT re-write `summary.md` or `PROJECT_MAP.md`. They are already "
        "correct; if you find an out-of-date detail, fix it through the trigger "
        "table (`add_note` / `add_decision` / `log_issue`) — never via direct file "
        "edit on summary.md.\n\n"
        "**Step 2 — Read these three files (or call the MCP equivalents):**\n\n"
        "| File | MCP tool | Purpose |\n"
        "| --- | --- | --- |\n"
        "| `.projectmem/AI_INSTRUCTIONS.md` | `get_instructions()` | Workflow rules (this file) |\n"
        "| `.projectmem/summary.md` | `get_summary()` | Distilled project memory |\n"
        "| `.projectmem/PROJECT_MAP.md` | `get_project_map()` | Structural layout |\n\n"
        "Prefer the MCP tools when available — they're cheaper (~500 tokens) than "
        "reading files individually and they auto-resolve the project root regardless "
        "of your working directory.\n\n"
        "**Step 3 — Check `.projectmem/issues/` only when a logged issue looks "
        "relevant to the current task** (use `get_issue(issue_id)` via MCP, or read "
        "the file). Don't read every issue on every session — that's wasteful.\n\n"
        "**Step 4 — Treat `.projectmem/events.jsonl` as the append-only raw log.** "
        "Do not edit it by hand unless repairing corruption. Use write tools.\n\n"
        "## MANDATORY Triggers — You MUST act on these automatically\n\n"
        "When a trigger fires, you MUST call the corresponding tool IMMEDIATELY, "
        "before continuing any other work. **Prefer MCP tools** (left column) when "
        "you're connected via an MCP-capable client; **fall back to CLI** (right "
        "column) otherwise.\n\n"
        "| Trigger | MCP tool | CLI command |\n"
        "| --- | --- | --- |\n"
        "| Bug, error, or unexpected behavior | `log_issue(summary, location)` | `pjm log \"<text>\" --at \"<file:line>\"` |\n"
        "| Fix attempt FAILED | `record_attempt(summary, outcome=\"failed\")` | `pjm attempt \"<text>\" --failed --at \"<file:line>\"` |\n"
        "| Fix attempt PARTIAL (helped but didn't fully fix) | `record_attempt(summary, outcome=\"partial\")` | `pjm attempt \"<text>\" --partial --at \"<file:line>\"` |\n"
        "| Fix attempt WORKED | `record_attempt(summary, outcome=\"worked\")` | `pjm attempt \"<text>\" --worked --at \"<file:line>\"` |\n"
        "| Fix confirmed — close the issue | `record_fix(summary)` | `pjm fix \"<text>\" --at \"<file:line>\"` |\n"
        "| Architectural / design decision | `add_decision(summary)` | `pjm decision \"<text>\" --at \"<file:line>\"` |\n"
        "| Gotcha / setup detail / constraint discovered | `add_note(summary)` | `pjm note \"<text>\" --at \"<file:line>\"` |\n"
        "| Before finishing the session | `get_summary()` | `pjm show` |\n\n"
        "All write tools auto-append to `events.jsonl` AND auto-regenerate "
        "`summary.md`. You do NOT need to call a separate \"save\" or \"regenerate\" "
        "command after each tool. The summary follows the events automatically.\n\n"
        "## Execution Rules\n\n"
        "1. **Log BEFORE you fix.** When you see a bug, call `log_issue` (or "
        "`pjm log`) BEFORE writing fix code. The issue survives interruptions and "
        "session boundaries; in-flight fix work does not.\n"
        "2. **Record IMMEDIATELY after each attempt.** Do not batch multiple attempts "
        "into one entry. Each distinct approach gets its own `record_attempt` call.\n"
        "3. **Close with `record_fix` only after evidence.** Test passes, error is "
        "gone, or the user confirms — anything less and the issue stays open.\n"
        "4. **Never skip logging because it feels minor.** A small fix today is a "
        "mystery regression tomorrow. Log it.\n"
        "5. **NEVER edit `.projectmem/summary.md` or `.projectmem/events.jsonl` "
        "directly via filesystem write.** Both are derived/append-only. Use the "
        "write tools. (You MAY edit `PROJECT_MAP.md` directly when restructuring it; "
        "it's not derived from events.)\n\n"
        "## What to track\n\n"
        "Use projectmem to preserve the development story that would otherwise be "
        "lost between chats, terminal sessions, and commits.\n\n"
        "Track:\n\n"
        "- new issues, bugs, regressions, unclear behavior, or investigation topics\n"
        "- hypotheses about causes\n"
        "- attempted fixes or experiments (each as its own `record_attempt`)\n"
        "- whether each attempt worked, failed, or partially helped\n"
        "- final fixes and the files involved\n"
        "- architectural, product, or implementation decisions and their reasons\n"
        "- gotchas, setup requirements, flaky tests, environment notes, "
        "important constraints\n"
        "- key files future contributors or AI agents should read first\n\n"
        "Do NOT track secrets, credentials, private customer data, access tokens, "
        "or large transcripts.\n\n"
        "## Auto-Capture (active)\n\n"
        "Git hooks installed by `pjm init` automatically capture:\n\n"
        "- Commits (post-commit hook)\n"
        "- Reverts (auto-classified as failed approaches)\n"
        "- Merges (auto-classified as milestones)\n"
        "- File churn (the `pjm watch` daemon flags rapid same-file edits)\n\n"
        "You do NOT need to manually log any of those. You SHOULD still manually log:\n\n"
        "- Decisions with rationale (`add_decision` / `pjm decision`)\n"
        "- Pre-attempt context for complex fixes (`record_attempt` / `pjm attempt`)\n"
        "- External factors and gotchas (`add_note` / `pjm note`)\n"
        "- Failure context that commit messages don't capture\n\n"
        "## Pre-commit safety net\n\n"
        "Every `git commit` automatically runs `pjm precheck` against the staged "
        "files. If you're about to commit a file with unresolved issues, recent "
        "failed attempts, or high churn, you'll see a warning block before the "
        "commit lands. Read it; it exists to stop you from repeating known "
        "failures. To bypass once: `git commit --no-verify`.\n\n"
        "## Rules summary\n\n"
        "- **MANDATORY: Log before you exit.** Work is not finished until project "
        "memory reflects what happened.\n"
        "- **MANDATORY: Record failed and partial attempts.** Negative and "
        "partial-credit knowledge is often the most valuable part of project memory.\n"
        "- Keep entries concise but specific enough that another person or AI can "
        "avoid repeating work. Include file paths, error names, test names.\n"
        "- Prefer several small accurate entries over one vague long entry.\n"
        "- Do not claim something is fixed until tests, reproduction, or user "
        "confirmation supports it.\n"
        "- Do not overwrite history. `events.jsonl` is append-only; `summary.md` "
        "is derived from it.\n"
        "- If MCP is unavailable, use the CLI (`pjm log`, `pjm attempt`, "
        "`pjm fix`, `pjm decision`, `pjm note`). If neither is available, clearly "
        "tell the user what should be recorded.\n"
        "- **`pjm` is the canonical CLI command** (since v0.0.4). The legacy "
        "`projectmem` alias still works if installed.\n\n"
        "## Minimal prompt for AI tools (Universal Mode)\n\n"
        "Read `.projectmem/AI_INSTRUCTIONS.md`, `.projectmem/summary.md`, and "
        "`.projectmem/PROJECT_MAP.md` before working. This project uses mandatory "
        "memory tracking with auto-capture enabled. If summary.md contains "
        "placeholder text, populate it via `pjm decision` and `pjm note` (or the "
        "`add_decision` / `add_note` MCP tools) — never edit summary.md directly. "
        "Git hooks log commits, reverts, and merges automatically. You MUST still "
        "run `pjm log` when you find a bug, `pjm attempt` for fix attempts, "
        "`pjm fix` when confirmed, and `pjm decision` for architectural choices. "
        "Skipping these steps means your work is incomplete.\n"
    )


def initial_project_map(root: Path) -> str:
    project_name = root.name
    return (
        f"# Project Map - {project_name}\n\n"
        "Status: not created yet\n\n"
        "This file should be created by the first AI assistant or developer who "
        "works in this project after `projectmem init`.\n\n"
        "## Instructions for AI assistants\n\n"
        "1. Read `.projectmem/AI_INSTRUCTIONS.md` first.\n"
        "2. Inspect the project structure, package metadata, README, tests, and "
        "obvious entry points.\n"
        "3. Replace this placeholder with a useful project map.\n"
        "4. Do not recreate this file from scratch in later sessions unless it is "
        "clearly stale or the user asks.\n"
        "5. Update this file when folders, entry points, routes, commands, or "
        "important relationships change.\n\n"
        "## Suggested shape\n\n"
        "```markdown\n"
        "# Project Map - project-name\n\n"
        "Status: created\n\n"
        "## Project purpose\n"
        "Short description of what the project does.\n\n"
        "## Main folders\n"
        "- `src/` - package or application code\n"
        "- `tests/` - test suite\n\n"
        "## Entry points\n"
        "- `module.path:main` - CLI or app entry point\n\n"
        "## Important files\n"
        "- `pyproject.toml` - package metadata and scripts\n"
        "- `README.md` - user-facing overview\n\n"
        "## Relationships\n"
        "- `cli.py` calls command modules in `commands/`\n\n"
        "## Suggested first reads\n"
        "1. `README.md`\n"
        "2. `pyproject.toml`\n"
        "```\n"
    )


def ensure_gitignore_entry(root: Path) -> None:
    """Add projectmem's runtime + scratch files to .gitignore.

    Default policy: commit distilled team knowledge (summary.md, PROJECT_MAP.md,
    AI_INSTRUCTIONS.md, issues/), ignore the raw log + runtime files.
    For total privacy, users can manually add `.projectmem/` to their .gitignore.
    """
    gitignore = root / ".gitignore"
    entries = [
        f"{MEM_DIR}/{EVENTS_FILE}",
        f"{MEM_DIR}/watch.pid",
        f"{MEM_DIR}/watch.log",
    ]
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    existing_lines = existing.splitlines()
    new_entries = [e for e in entries if e not in existing_lines]
    if not new_entries:
        return
    prefix = "" if not existing_lines or existing_lines[-1] == "" else "\n"
    gitignore.write_text(existing + prefix + "\n".join(new_entries) + "\n", encoding="utf-8")


def read_events(root: Path | None = None) -> list[Event]:
    path = events_path(root)
    events: list[Event] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            events.append(Event.from_dict(json.loads(line)))
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            raise ProjectMemError(f"Invalid event at {path}:{line_number}: {exc}") from exc
    return events


def append_event(event: Event, root: Path | None = None) -> Event:
    # Scrub known secret patterns out of user-supplied text fields BEFORE
    # they touch disk. "100% local" implies "100% your responsibility" only
    # if we let secrets leak through; default-on redaction is the trust
    # contract. Opt out via ``PROJECTMEM_NO_REDACT=1``.
    try:
        from projectmem.redaction import redact_event_fields

        fired = redact_event_fields(event)
        if fired:
            kinds = ", ".join(sorted(set(fired)))
            print(
                f"projectmem: redacted {len(fired)} secret(s) before write ({kinds})",
                file=sys.stderr,
            )
    except Exception:
        # Redaction is a guardrail, not a gatekeeper — if anything goes
        # wrong inside the scrubber we still write the event. Better a
        # logged secret than a lost event in a tool whose job is logging.
        pass

    path = events_path(root)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event.to_dict(), sort_keys=True) + "\n")

    # Auto-promote library-mentioning attempts/decisions/notes to the
    # machine-wide global store so projects with overlapping stacks inherit
    # the lesson. We pass the project's detected libraries so the promotion
    # is filtered by the current stack — a vite project mentioning "next"
    # in plain English won't surface a fake Next.js gotcha to other projects.
    # Failures here are non-fatal — the local event is already persisted;
    # only the optional cross-project propagation is at risk.
    if event.type in ("attempt", "decision", "note") and event.summary:
        try:
            from projectmem.global_memory import auto_promote_event, detect_stack

            project_root = root or Path.cwd()
            stack = detect_stack(project_root)
            auto_promote_event(
                event_summary=event.summary,
                event_type=event.type,
                project_name=project_root.name,
                project_libraries=stack.get("libraries", []) + stack.get("tags", []),
                outcome=getattr(event, "outcome", None),
            )
        except Exception:
            # Auto-promote is a best-effort enrichment. Never let it break
            # the primary write path.
            pass

    return event


def next_issue_id(events: list[Event]) -> str:
    issue_ids = [
        int(event.issue_id)
        for event in events
        if event.type == "issue" and event.issue_id and event.issue_id.isdigit()
    ]
    return f"{(max(issue_ids) if issue_ids else 0) + 1:04d}"


def current_issue_id(events: list[Event]) -> str | None:
    closed = {event.issue_id for event in events if event.type == "fix" and event.issue_id}
    for event in reversed(events):
        if event.type == "issue" and event.issue_id not in closed:
            return event.issue_id
    return None


CURRENT_ISSUE_MARKER = ".current_issue"


def current_issue_marker_path(root: Path | None = None) -> Path:
    return require_mem_dir(root) / CURRENT_ISSUE_MARKER


def write_current_issue(issue_id: str, root: Path | None = None) -> None:
    """Persist the active issue ID. Cleared on `pjm fix`."""
    try:
        current_issue_marker_path(root).write_text(issue_id, encoding="utf-8")
    except OSError:
        pass  # marker is advisory; don't fail the command


def read_current_issue(root: Path | None = None) -> str | None:
    """Read the active issue ID, if any. Returns None if no marker present."""
    try:
        path = current_issue_marker_path(root)
    except ProjectMemError:
        return None
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def clear_current_issue(root: Path | None = None) -> None:
    """Clear the active-issue marker. No-op if it does not exist."""
    try:
        path = current_issue_marker_path(root)
    except ProjectMemError:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def latest_open_issue_within(
    events: list[Event], minutes: int = 5
) -> str | None:
    """Return the most recent OPEN issue ID iff it was opened within `minutes`.

    Used as a time-fenced fallback for `pjm attempt` when no current-issue
    marker exists — avoids silently attaching an attempt to a stale issue.
    """
    from datetime import datetime, timezone, timedelta
    closed = {e.issue_id for e in events if e.type == "fix" and e.issue_id}
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    for event in reversed(events):
        if event.type != "issue" or event.issue_id in closed:
            continue
        try:
            ts = datetime.fromisoformat(event.timestamp.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None
        if ts >= cutoff:
            return event.issue_id
        return None  # most recent open is older than the window — no auto-attach
    return None


def get_git_commit(root: Path | None = None) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root or Path.cwd(),
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    commit = result.stdout.strip()
    return commit or None
