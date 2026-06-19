"""Pre-commit warnings — the killer feature.

Compares staged changes against project memory and surfaces warnings BEFORE
the developer commits. This is the unique differentiator: nobody else can
warn you about repeating your own mistakes because nobody else has the
memory layer.

Usage:
    pjm precheck                          # Check staged files (default)
    pjm precheck --working                # Check working tree (not staged)
    pjm precheck --files X Y              # Check specific files
    pjm precheck --level info|warn|block  # Strictness
    pjm precheck --quiet                  # Only show warnings
    pjm precheck --snooze 2h              # Silence warnings for a while
    pjm precheck --unsnooze               # Re-enable warnings now
"""
from __future__ import annotations

import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import typer

from projectmem.models import Event
from projectmem.storage import MEM_DIR, read_events, require_mem_dir


# ── Thresholds ──
HIGH_CHURN_THRESHOLD = 4         # changes in CHURN_WINDOW_COMMITS to trigger
CHURN_WINDOW_COMMITS = 7         # rolling window for churn detection
FAILED_ATTEMPT_BLOCK_COUNT = 3   # 3+ failed attempts → block (at --level block)
RECENT_DAYS = 30                 # only consider events newer than this

# ── Severity ──
SEVERITY_INFO = "info"
SEVERITY_WARN = "warn"
SEVERITY_BLOCK = "block"

SEVERITY_LEVELS = {SEVERITY_INFO: 0, SEVERITY_WARN: 1, SEVERITY_BLOCK: 2}

# ── Snooze (0.1.4) ──
# A TTL'd marker file silences precheck output without uninstalling the
# hook. Unlike `git commit --no-verify`, a snooze is itself recorded in the
# event log, so even the silence is auditable. Expired markers are removed
# on the next run.
SNOOZE_MARKER = "precheck.snooze"
_DURATION_RE = re.compile(r"^(\d+)\s*([mhd])$", re.IGNORECASE)
_DURATION_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


def _stdout_encoding() -> str:
    """Return the active stdout encoding, falling back to UTF-8."""
    return getattr(sys.stdout, "encoding", None) or "utf-8"


def _console_safe(text: object) -> str:
    """Return text that can be printed by the current console encoding."""
    value = str(text)
    encoding = _stdout_encoding()
    try:
        value.encode(encoding)
        return value
    except UnicodeEncodeError:
        return value.encode(encoding, errors="replace").decode(encoding)


def _safe_echo(text: object = "", *, err: bool = False) -> None:
    typer.echo(_console_safe(text), err=err)


def _rule(width: int = 60) -> str:
    encoding = _stdout_encoding().lower()
    if "utf" in encoding:
        return "─" * width
    return "-" * width


def parse_snooze_duration(text: str) -> timedelta:
    """Parse '30m' / '2h' / '1d' into a timedelta. Raises ValueError."""
    match = _DURATION_RE.match(text.strip())
    if not match:
        raise ValueError(
            f"Invalid duration '{text}' — use forms like 30m, 2h, or 1d."
        )
    value, unit = int(match.group(1)), match.group(2).lower()
    if value <= 0:
        raise ValueError("Snooze duration must be positive.")
    return timedelta(**{_DURATION_UNITS[unit]: value})


def _snooze_path(root: Path | None = None) -> Path:
    return require_mem_dir(root) / SNOOZE_MARKER


def active_snooze(root: Path | None = None) -> datetime | None:
    """Return the snooze expiry if one is active; clean up expired markers."""
    try:
        path = _snooze_path(root)
    except Exception:
        return None
    if not path.exists():
        return None
    try:
        expiry = datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        path.unlink(missing_ok=True)
        return None
    if expiry <= datetime.now(timezone.utc):
        path.unlink(missing_ok=True)
        return None
    return expiry


def set_snooze(duration_text: str, root: Path | None = None) -> datetime:
    """Write the snooze marker and log an audit note. Returns the expiry."""
    delta = parse_snooze_duration(duration_text)
    expiry = datetime.now(timezone.utc).replace(microsecond=0) + delta
    _snooze_path(root).write_text(expiry.isoformat(), encoding="utf-8")
    # The audit note is best-effort: a snooze must work even if the event
    # write fails, but when it succeeds the silence itself is on record.
    try:
        from projectmem.storage import append_event

        append_event(
            Event(
                type="note",
                summary=f"precheck warnings snoozed for {duration_text} "
                        f"(until {expiry.isoformat()})",
            ),
            root,
        )
    except Exception:
        pass
    return expiry


def clear_snooze(root: Path | None = None) -> bool:
    """Remove the snooze marker. Returns True if one existed."""
    try:
        path = _snooze_path(root)
    except Exception:
        return False
    existed = path.exists()
    path.unlink(missing_ok=True)
    return existed


def _remaining(expiry: datetime) -> str:
    delta = expiry - datetime.now(timezone.utc)
    minutes = max(1, int(delta.total_seconds() // 60))
    if minutes < 60:
        return f"{minutes}m"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h{mins:02d}m" if mins else f"{hours}h"


def run(
    level: str = SEVERITY_WARN,
    working: bool = False,
    files: list[str] | None = None,
    quiet: bool = False,
    root: Path | None = None,
    snooze: str | None = None,
    unsnooze: bool = False,
) -> None:
    """Run the pre-commit check."""
    root_path = root or Path.cwd()

    # Guard: only run if .projectmem exists
    if not (root_path / MEM_DIR).exists():
        return

    # Snooze management actions short-circuit the check itself.
    if unsnooze:
        if clear_snooze(root):
            _safe_echo("projectmem: precheck warnings re-enabled.")
        else:
            _safe_echo("projectmem: no active snooze.")
        return
    if snooze:
        try:
            expiry = set_snooze(snooze, root)
        except ValueError as exc:
            _safe_echo(f"Error: {exc}", err=True)
            raise typer.Exit(1)
        _safe_echo(
            f"projectmem: precheck warnings snoozed until "
            f"{expiry.strftime('%H:%M UTC')} (logged to memory). "
            f"Re-enable early with `pjm precheck --unsnooze`."
        )
        return

    # Honor an active snooze: stay quiet, but say so in one dim line so a
    # silenced warning is never mistaken for a clean check.
    expiry = active_snooze(root)
    if expiry is not None:
        _safe_echo(
            f"\033[2mprojectmem: warnings snoozed ({_remaining(expiry)} left) — "
            f"`pjm precheck --unsnooze` to re-enable.\033[0m"
        )
        return

    # Determine which files to check
    if files:
        target_files = files
    elif working:
        target_files = _get_working_tree_files(root_path)
    else:
        target_files = _get_staged_files(root_path)

    if not target_files:
        if not quiet:
            _safe_echo("projectmem: No files to check.")
        return

    # Read events and build warnings
    try:
        events = read_events(root_path)
    except Exception:
        return  # Silent if memory can't be read

    warnings = _analyze_files(target_files, events, root=root_path)

    if not warnings:
        if not quiet:
            _safe_echo("\033[32mprojectmem:\033[0m no warnings — looking good!")
        return

    # Render warnings
    has_blocking = any(w["severity"] == SEVERITY_BLOCK for w in warnings)
    _render_warnings(warnings, level)

    # Exit with non-zero if blocking and level is "block"
    if has_blocking and level == SEVERITY_BLOCK:
        raise typer.Exit(1)


def _analyze_files(
    files: list[str], events: list[Event], root: Path | None = None
) -> list[dict[str, Any]]:
    """Analyze each file against project memory, return warnings."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=RECENT_DAYS)

    warnings: list[dict[str, Any]] = []

    # ── Check 6 input: stale memories (computed once for all files) ──
    # Decisions/fixes/notes whose cited file changed substantially after
    # they were logged. Never deleted, never down-ranked — flagged for a
    # human (or agent) to confirm or supersede.
    try:
        from projectmem.staleness import find_stale_events

        stale_by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in find_stale_events(events, root):
            stale_by_file[item["file"]].append(item)
    except Exception:
        stale_by_file = defaultdict(list)

    for file_path in files:
        # Find all events referencing this file
        file_events = _events_for_file(file_path, events)

        if not file_events:
            continue

        # Filter to recent
        recent = []
        for e in file_events:
            try:
                ts = datetime.fromisoformat(e.timestamp.replace("Z", "+00:00"))
                if ts >= cutoff:
                    recent.append(e)
            except (ValueError, AttributeError):
                recent.append(e)

        # ── Check 1: Failed attempts ──
        failed_attempts = [
            e for e in recent if e.type == "attempt" and e.outcome == "failed"
        ]
        if failed_attempts:
            count = len(failed_attempts)
            severity = SEVERITY_BLOCK if count >= FAILED_ATTEMPT_BLOCK_COUNT else SEVERITY_WARN
            # List the dead ends themselves (0.1.4), not just a count — the
            # whole point of memory-of-failure is telling the next session
            # WHAT not to retry. Up to 3 most recent, newest first.
            details = []
            for attempt in reversed(failed_attempts[-3:]):
                details.append(
                    f"x {attempt.summary[:90]} ({_age(attempt.timestamp)})"
                )
            if count > 3:
                details.append(f"  ... and {count - 3} more (pjm search --failed-only)")
            warnings.append({
                "file": file_path,
                "severity": severity,
                "type": "failed_attempts",
                "title": f"What already failed here ({count} attempt{'s' if count != 1 else ''}):",
                "details": details,
            })

        # ── Check 2: Open issues ──
        open_issues = _find_open_issues(file_events, events)
        if open_issues:
            warnings.append({
                "file": file_path,
                "severity": SEVERITY_WARN,
                "type": "open_issues",
                "title": f"{len(open_issues)} unresolved issue{'s' if len(open_issues) != 1 else ''} on this file",
                "details": [
                    f"#{issue.issue_id}: {issue.summary[:80]}"
                    for issue in open_issues[:3]
                ],
            })

        # ── Check 3: High churn ──
        # Source of truth is `git log` over the window, not the event log
        # (L-023a). Counting events would understate fresh, repeated edits
        # that the memory layer hasn't captured yet.
        git_churn = _git_recent_changes(file_path, RECENT_DAYS)
        churn_count = git_churn if git_churn is not None else sum(
            1 for e in recent if e.git_commit
        )
        if churn_count >= HIGH_CHURN_THRESHOLD:
            warnings.append({
                "file": file_path,
                "severity": SEVERITY_WARN,
                "type": "high_churn",
                "title": f"HIGH CHURN: {churn_count} changes in last {RECENT_DAYS} days",
                "details": [
                    "May indicate unresolved architectural issue",
                ],
            })

        # ── Check 4: Recent reverts ──
        reverts = [
            e for e in recent
            if e.type == "attempt" and e.outcome == "failed"
            and e.capture_source == "git_post_revert"
        ]
        if reverts:
            last_revert = reverts[-1]
            warnings.append({
                "file": file_path,
                "severity": SEVERITY_WARN,
                "type": "recent_revert",
                "title": "Recent revert affected this file",
                "details": [
                    f"Reverted: {last_revert.git_message or last_revert.summary[:80]}",
                    f"  ({_age(last_revert.timestamp)})",
                ],
            })

        # ── Check 5: Recent decisions ──
        decisions = [e for e in recent if e.type == "decision"]
        if decisions:
            last = decisions[-1]
            warnings.append({
                "file": file_path,
                "severity": SEVERITY_INFO,
                "type": "relevant_decision",
                "title": "Recent decision affects this file",
                "details": [
                    f"{last.summary[:100]}",
                    f"  ({_age(last.timestamp)})",
                ],
            })

        # ── Check 6: Possibly-stale memories (0.1.4) ──
        stale_items = stale_by_file.get(file_path, [])
        if stale_items:
            details = []
            for item in stale_items[:3]:
                event = item["event"]
                if item["commits_since"] == -1:
                    reason = "cited file no longer exists"
                else:
                    reason = f"predates {item['commits_since']} commits to this file"
                details.append(
                    f"{event.type} [{event.id}] \"{event.summary[:70]}\" — {reason}"
                )
            details.append(
                "Confirm it still holds, or retire it: "
                "pjm decision \"...\" --supersedes <id>"
            )
            warnings.append({
                "file": file_path,
                "severity": SEVERITY_WARN,
                "type": "possibly_stale",
                "title": (
                    f"{len(stale_items)} possibly-stale memories cite this file"
                    if len(stale_items) != 1
                    else "1 possibly-stale memory cites this file"
                ),
                "details": details,
            })

    return warnings


def _events_for_file(file_path: str, events: list[Event]) -> list[Event]:
    """Return all events that reference this file."""
    matching: list[Event] = []
    for e in events:
        # Direct files list
        if file_path in e.files:
            matching.append(e)
            continue
        # Location field
        if e.location:
            loc_file = e.location.split(":")[0]
            if loc_file == file_path:
                matching.append(e)
                continue
        # Summary mention
        if file_path in e.summary:
            matching.append(e)
    return matching


def _find_open_issues(
    file_events: list[Event], all_events: list[Event]
) -> list[Event]:
    """Find issues on this file that haven't been fixed."""
    issues = [e for e in file_events if e.type == "issue"]
    if not issues:
        return []

    # Find fix IDs to exclude resolved issues
    resolved_ids = {
        e.issue_id for e in all_events if e.type == "fix" and e.issue_id
    }

    return [i for i in issues if i.issue_id not in resolved_ids]


def _render_warnings(warnings: list[dict[str, Any]], level: str) -> None:
    """Render warnings to terminal with colors."""
    bold = "\033[1m"
    dim = "\033[2m"
    yellow = "\033[33m"
    red = "\033[31m"
    cyan = "\033[36m"
    reset = "\033[0m"

    severity_threshold = SEVERITY_LEVELS.get(level, 1)

    # Filter by severity level
    visible = [
        w for w in warnings
        if SEVERITY_LEVELS.get(w["severity"], 1) >= severity_threshold - 1  # always show at level-1+
    ]
    # Always show warn+ regardless of level
    visible = [
        w for w in warnings
        if SEVERITY_LEVELS.get(w["severity"], 1) >= 1  # warn or block
        or level == SEVERITY_INFO
    ]

    if not visible:
        return

    _safe_echo("")
    _safe_echo(f"{bold}projectmem: Pre-Commit Check{reset}")
    _safe_echo(f"{dim}{_rule(60)}{reset}")
    _safe_echo("")

    # Group by file
    by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for w in visible:
        by_file[w["file"]].append(w)

    for file_path, file_warnings in by_file.items():
        _safe_echo(f"  {bold}{file_path}{reset}")
        for w in file_warnings:
            if w["severity"] == SEVERITY_BLOCK:
                icon = f"{red}BLOCK{reset}"
            elif w["severity"] == SEVERITY_WARN:
                icon = f"{yellow}WARN{reset}"
            else:
                icon = f"{cyan}INFO{reset}"
            _safe_echo(f"    {icon}  {w['title']}")
            for detail in w["details"]:
                _safe_echo(f"           {dim}{detail}{reset}")
        _safe_echo("")

    _safe_echo(f"{dim}{_rule(60)}{reset}")

    blocking = sum(1 for w in visible if w["severity"] == SEVERITY_BLOCK)
    warning = sum(1 for w in visible if w["severity"] == SEVERITY_WARN)

    if blocking and level == SEVERITY_BLOCK:
        _safe_echo(f"{red}Blocked: {blocking} critical warning(s).{reset}")
        _safe_echo("  Bypass once: git commit --no-verify")
    elif warning or blocking:
        _safe_echo(
            f"{dim}{warning + blocking} warning(s). Review before committing.{reset}"
        )
    _safe_echo("")


def _get_staged_files(root: Path) -> list[str]:
    """Get list of files staged for commit."""
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []


def _git_recent_changes(file_path: str, days: int, root: Path | None = None) -> int | None:
    """Count commits touching `file_path` in the last `days` days.

    Returns None if git is unavailable or the call fails — caller falls back
    to event-log counting.
    """
    try:
        result = subprocess.run(
            ["git", "log", f"--since={days}.days.ago", "--oneline", "--", file_path],
            cwd=root or Path.cwd(),
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return sum(1 for line in result.stdout.splitlines() if line.strip())


def _get_working_tree_files(root: Path) -> list[str]:
    """Get list of modified files in working tree (not yet staged)."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []


def _age(timestamp: str) -> str:
    """Convert timestamp to human-readable age."""
    try:
        ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = now - ts
        days = delta.days
        if days == 0:
            hours = delta.seconds // 3600
            return "just now" if hours == 0 else f"{hours}h ago"
        if days == 1:
            return "yesterday"
        if days < 7:
            return f"{days}d ago"
        if days < 30:
            return f"{days // 7}w ago"
        return f"{days // 30}mo ago"
    except (ValueError, AttributeError):
        return "unknown"
