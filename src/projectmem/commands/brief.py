"""Session-start briefing (0.1.4) — `pjm brief`.

One screen that answers "where was I?": active failure warnings,
possibly-stale memories, open issues, the latest live decisions, the top
stack-relevant gotchas, and the prevention score with a week-over-week
delta. Composes data the other commands already compute — no new state,
no daemon, nothing leaves the machine.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import typer

from projectmem.models import Event, superseded_ids
from projectmem.storage import read_events

RECENT_DAYS = 30
MAX_DECISIONS = 5
MAX_GOTCHAS = 3
MAX_STALE = 5

_BOLD = "\033[1m"
_DIM = "\033[2m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_TEAL = "\033[36m"
_GREEN = "\033[32m"
_RESET = "\033[0m"


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


def _safe_echo(text: object = "") -> None:
    typer.echo(_console_safe(text))


def _rule(width: int = 60) -> str:
    encoding = _stdout_encoding().lower()
    if "utf" in encoding:
        return "─" * width
    return "-" * width


def run(root: Path | None = None) -> None:
    root_path = root or Path.cwd()
    events = read_events(root)

    _safe_echo("")
    _safe_echo(f"{_BOLD}projectmem brief — {root_path.name}{_RESET}")
    _safe_echo(f"{_DIM}{_rule(60)}{_RESET}")

    _section_warnings(events)
    _section_stale(events, root_path)
    _section_open_issues(events)
    _section_decisions(events)
    _section_gotchas(root_path)
    _section_score(events)

    _safe_echo(f"{_DIM}{_rule(60)}{_RESET}")
    _safe_echo("")


def _recent(events: list[Event], days: int = RECENT_DAYS) -> list[Event]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for e in events:
        try:
            ts = datetime.fromisoformat(e.timestamp.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if ts >= cutoff:
            out.append(e)
    return out


def _file_of(event: Event) -> str | None:
    if event.location:
        return event.location.split(":")[0]
    if event.files:
        return event.files[0]
    return None


def _section_warnings(events: list[Event]) -> None:
    failed = [
        e for e in _recent(events)
        if e.type == "attempt" and e.outcome == "failed"
    ]
    by_file: dict[str, list[Event]] = defaultdict(list)
    for e in failed:
        by_file[_file_of(e) or "(no file)"].append(e)
    _safe_echo(f"{_YELLOW}⚠ Active warnings{_RESET}")
    if not by_file:
        _safe_echo(f"   {_DIM}none — no failed attempts in {RECENT_DAYS} days{_RESET}")
        return
    for file_path, items in sorted(by_file.items(), key=lambda kv: -len(kv[1]))[:4]:
        last = items[-1]
        _safe_echo(
            f"   {file_path} — {len(items)} failed attempt"
            f"{'s' if len(items) != 1 else ''} "
            f"{_DIM}(last: {last.summary[:60]}){_RESET}"
        )


def _section_stale(events: list[Event], root: Path) -> None:
    try:
        from projectmem.staleness import find_stale_events

        stale = find_stale_events(events, root)
    except Exception:
        stale = []
    if not stale:
        return  # silence is the right default — no flags, no noise
    _safe_echo(f"{_YELLOW}⏳ Possibly stale{_RESET}")
    for item in stale[:MAX_STALE]:
        event = item["event"]
        if item["commits_since"] == -1:
            reason = f"{item['file']} no longer exists"
        else:
            reason = f"{item['file']} changed {item['commits_since']}x since"
        _safe_echo(
            f"   {event.type} [{event.id}] {event.summary[:55]} "
            f"{_DIM}— {reason}{_RESET}"
        )
    _safe_echo(
        f"   {_DIM}confirm, or retire: pjm decision \"...\" --supersedes <id>{_RESET}"
    )


def _section_open_issues(events: list[Event]) -> None:
    fixed = {e.issue_id for e in events if e.type == "fix" and e.issue_id}
    open_issues = [
        e for e in events if e.type == "issue" and e.issue_id not in fixed
    ]
    _safe_echo(f"{_RED}📋 Open issues{_RESET}")
    if not open_issues:
        _safe_echo(f"   {_DIM}none open{_RESET}")
        return
    for issue in open_issues[-4:]:
        _safe_echo(f"   #{issue.issue_id} {issue.summary[:70]}")


def _section_decisions(events: list[Event]) -> None:
    retired = superseded_ids(events)
    live = [
        e for e in events if e.type == "decision" and e.id not in retired
    ]
    _safe_echo(f"{_TEAL}🕑 Recent decisions{_RESET}")
    if not live:
        _safe_echo(f"   {_DIM}none logged yet{_RESET}")
        return
    for d in live[-MAX_DECISIONS:]:
        _safe_echo(f"   {d.summary[:75]}")


def _section_gotchas(root: Path) -> None:
    try:
        from projectmem.global_memory import detect_stack, get_relevant_entries

        stack = detect_stack(root)
        gotchas = get_relevant_entries(stack).get("gotchas", [])
    except Exception:
        gotchas = []
    if not gotchas:
        return
    _safe_echo(f"{_TEAL}💡 Stack gotchas{_RESET}")
    for g in gotchas[-MAX_GOTCHAS:]:
        lib = g.get("library", "")
        text = g.get("text") or g.get("summary") or ""
        _safe_echo(f"   {lib}: {text[:70]}")


def _section_score(events: list[Event]) -> None:
    try:
        from projectmem.commands.score import calculate_score

        dicts = [e.to_dict() for e in events]
        current = calculate_score(dicts)
        week_ago = datetime.now(timezone.utc) - timedelta(days=7)
        older = []
        for d in dicts:
            try:
                ts = datetime.fromisoformat(
                    str(d.get("timestamp", "")).replace("Z", "+00:00")
                )
            except ValueError:
                continue
            if ts < week_ago:
                older.append(d)
        previous = calculate_score(older)
        delta = current["score"] - previous["score"]
    except Exception:
        return
    if delta > 0:
        trend = f" {_GREEN}▲ +{delta} this week{_RESET}"
    elif delta < 0:
        trend = f" {_RED}▼ {delta} this week{_RESET}"
    else:
        trend = f" {_DIM}— unchanged this week{_RESET}"
    _safe_echo(
        f"{_GREEN}📈 Score{_RESET}  {current['grade']} ({current['score']}/100){trend}"
    )
