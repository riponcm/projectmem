"""Failure Prevention Score — provable ROI metric.

Calculates a quantifiable score based on project memory data:
- Failed approaches on record
- Decisions documented
- Debugging hours saved
- Token waste prevented
- Files with known gotchas
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import typer

from projectmem.models import location_to_file
from projectmem.storage import read_events, require_mem_dir


# ── Weights for time/token estimation ──
HOURS_PER_FAILED_APPROACH = 0.5    # 30 min saved per documented dead-end
HOURS_PER_FIX_WITH_CONTEXT = 0.25  # 15 min saved per fix with file context
HOURS_PER_DECISION = 0.15          # 9 min saved per documented decision
HOURS_PER_CHURN_FLAG = 0.75        # 45 min saved per churn alert
TOKENS_PER_FAILED_APPROACH = 3000  # tokens to rediscover a dead-end
TOKENS_PER_CONTEXT_REBUILD = 2000  # tokens to rebuild context from scratch
TOKENS_PER_DECISION = 500          # tokens to re-derive a decision
USD_PER_MILLION_TOKENS = 10.0      # average $/1M tokens


def calculate_score(events: list[dict[str, Any]], since_days: int | None = None) -> dict[str, Any]:
    """Calculate the prevention score from raw event dicts."""
    now = datetime.now(timezone.utc)

    # Filter by time window if specified
    filtered = events
    if since_days is not None:
        cutoff = now - timedelta(days=since_days)
        filtered = []
        for e in events:
            ts = e.get("timestamp", "")
            try:
                event_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if event_time >= cutoff:
                    filtered.append(e)
            except (ValueError, AttributeError):
                filtered.append(e)  # include if timestamp can't be parsed

    # ── Component metrics ──
    failed_approaches = 0
    successful_attempts = 0
    fixes_with_context = 0
    decisions_documented = 0
    notes_count = 0
    auto_captured_count = 0
    manual_count = 0
    files_seen: set[str] = set()
    files_with_failures: set[str] = set()
    files_with_gotchas: set[str] = set()  # files with any associated event
    file_event_counts: dict[str, int] = defaultdict(int)

    for e in filtered:
        etype = e.get("type", "")
        outcome = e.get("outcome")
        is_auto = e.get("auto_captured", False)

        if is_auto:
            auto_captured_count += 1
        else:
            manual_count += 1

        # Track files
        event_files = list(e.get("files", []))
        loc_file = location_to_file(e.get("location"))
        if loc_file:
            event_files.append(loc_file)
        for f in event_files:
            files_seen.add(f)
            file_event_counts[f] += 1
            files_with_gotchas.add(f)

        if etype == "attempt":
            if outcome == "failed":
                failed_approaches += 1
                for f in event_files:
                    files_with_failures.add(f)
            elif outcome == "worked":
                successful_attempts += 1
        elif etype == "fix":
            fixes_with_context += 1
        elif etype == "decision":
            decisions_documented += 1
        elif etype == "note":
            notes_count += 1

    # ── Derived metrics ──
    debugging_hours_saved = round(
        (failed_approaches * HOURS_PER_FAILED_APPROACH)
        + (fixes_with_context * HOURS_PER_FIX_WITH_CONTEXT)
        + (decisions_documented * HOURS_PER_DECISION),
        1,
    )

    # Count high-churn files (4+ events)
    high_churn_files = sum(1 for c in file_event_counts.values() if c >= 4)
    debugging_hours_saved += round(high_churn_files * HOURS_PER_CHURN_FLAG, 1)

    tokens_saved = (
        (failed_approaches * TOKENS_PER_FAILED_APPROACH)
        + (fixes_with_context * TOKENS_PER_CONTEXT_REBUILD)
        + (decisions_documented * TOKENS_PER_DECISION)
        + (auto_captured_count * TOKENS_PER_CONTEXT_REBUILD)  # auto events = context that'd be rebuilt
    )

    usd_saved = round((tokens_saved / 1_000_000) * USD_PER_MILLION_TOKENS, 2)

    # ── Score calculation ──
    # Score components (0-20 each, max 100)
    failed_score = min(failed_approaches * 2, 20)
    decision_score = min(decisions_documented * 1.5, 20)
    fix_score = min(fixes_with_context * 2, 20)
    gotcha_score = min(len(files_with_gotchas) * 0.5, 20)
    coverage_score = min(
        (notes_count * 0.5)
        + (auto_captured_count * 0.3)
        + (successful_attempts * 1),
        20,
    )

    total_score = min(
        int(failed_score + decision_score + fix_score + gotcha_score + coverage_score),
        100,
    )

    # Letter grade
    if total_score >= 90:
        grade = "A+"
    elif total_score >= 80:
        grade = "A"
    elif total_score >= 60:
        grade = "B"
    elif total_score >= 40:
        grade = "C"
    elif total_score >= 20:
        grade = "D"
    else:
        grade = "F"

    return {
        "score": total_score,
        "grade": grade,
        "components": {
            "failed_approaches": failed_approaches,
            "successful_attempts": successful_attempts,
            "decisions_documented": decisions_documented,
            "fixes_with_context": fixes_with_context,
            "notes_count": notes_count,
            "files_with_gotchas": len(files_with_gotchas),
            "high_churn_files": high_churn_files,
        },
        "capture": {
            "auto_captured": auto_captured_count,
            "manual": manual_count,
            "total": len(filtered),
            "auto_rate": round(auto_captured_count / max(len(filtered), 1) * 100, 1),
        },
        "value": {
            "debugging_hours_saved": debugging_hours_saved,
            "tokens_saved": tokens_saved,
            "usd_saved": usd_saved,
        },
        "score_breakdown": {
            "failed_knowledge": round(failed_score, 1),
            "decisions": round(decision_score, 1),
            "fixes": round(fix_score, 1),
            "file_coverage": round(gotcha_score, 1),
            "general_coverage": round(coverage_score, 1),
        },
    }


def format_terminal(result: dict[str, Any]) -> str:
    """Format score as a terminal-friendly display."""
    s = result["score"]
    g = result["grade"]
    c = result["components"]
    v = result["value"]
    cap = result["capture"]
    bd = result["score_breakdown"]

    # Color for grade
    if s >= 80:
        grade_color = "\033[1;32m"  # bright green
    elif s >= 60:
        grade_color = "\033[1;36m"  # bright cyan
    elif s >= 40:
        grade_color = "\033[1;33m"  # bright yellow
    else:
        grade_color = "\033[1;31m"  # bright red
    reset = "\033[0m"
    dim = "\033[2m"
    bold = "\033[1m"

    bar = "=" * 44
    lines = [
        f"",
        f"  {dim}{bar}{reset}",
        f"  {bold}  projectmem Prevention Score{reset}",
        f"  {dim}{bar}{reset}",
        f"",
        f"      {grade_color}{'=' * 8} {g} {'=' * 8}{reset}",
        f"       {grade_color}Score: {s}/100{reset}",
        f"",
        f"  {dim}{'─' * 44}{reset}",
        f"  {bold}Knowledge Captured{reset}",
        f"  {dim}{'─' * 44}{reset}",
        f"    Failed approaches on record:  {bold}{c['failed_approaches']}{reset}",
        f"    Decisions documented:          {bold}{c['decisions_documented']}{reset}",
        f"    Fixes with context:            {bold}{c['fixes_with_context']}{reset}",
        f"    Notes recorded:                {bold}{c['notes_count']}{reset}",
        f"    Files with known gotchas:      {bold}{c['files_with_gotchas']}{reset}",
        f"    High-churn files flagged:      {bold}{c['high_churn_files']}{reset}",
        f"",
        f"  {dim}{'─' * 44}{reset}",
        f"  {bold}Estimated Value{reset}",
        f"  {dim}{'─' * 44}{reset}",
        f"    Debugging hours saved:       {bold}~{v['debugging_hours_saved']:.1f}h{reset}",
        f"    Tokens saved:                {bold}{v['tokens_saved']:,}{reset}",
        f"    Estimated USD saved:         {bold}${v['usd_saved']:.2f}{reset}",
        f"",
        f"  {dim}{'─' * 44}{reset}",
        f"  {bold}Capture Stats{reset}",
        f"  {dim}{'─' * 44}{reset}",
        f"    Total events:                {bold}{cap['total']}{reset}",
        f"    Manual:                      {bold}{cap['manual']}{reset}",
        f"    Auto-captured:               {bold}{cap['auto_captured']}{reset}",
        f"    Auto-capture rate:           {bold}{cap['auto_rate']}%{reset}",
        f"",
        f"  {dim}{'─' * 44}{reset}",
        f"  {bold}Score Breakdown (20 pts each){reset}",
        f"  {dim}{'─' * 44}{reset}",
        f"    Failed knowledge:    {_bar(bd['failed_knowledge'], 20)} {bd['failed_knowledge']}/20",
        f"    Decisions:           {_bar(bd['decisions'], 20)} {bd['decisions']}/20",
        f"    Fixes:               {_bar(bd['fixes'], 20)} {bd['fixes']}/20",
        f"    File coverage:       {_bar(bd['file_coverage'], 20)} {bd['file_coverage']}/20",
        f"    General coverage:    {_bar(bd['general_coverage'], 20)} {bd['general_coverage']}/20",
        f"  {dim}{bar}{reset}",
        f"",
    ]
    return "\n".join(lines)


def _bar(value: float, max_val: float, width: int = 16) -> str:
    """Render a small progress bar."""
    filled = int((value / max_val) * width) if max_val > 0 else 0
    return f"\033[36m{'█' * filled}{'░' * (width - filled)}\033[0m"


def format_badge(result: dict[str, Any]) -> str:
    """Generate a shields.io badge URL."""
    s = result["score"]
    g = result["grade"]
    if s >= 80:
        color = "brightgreen"
    elif s >= 60:
        color = "green"
    elif s >= 40:
        color = "yellow"
    else:
        color = "red"
    return f"https://img.shields.io/badge/projectmem-Score%20{s}%20({g})-{color}"


def run(
    fmt: str = "text",
    since: str | None = None,
    verbose: bool = False,
    root: Path | None = None,
) -> None:
    """Calculate and display the prevention score."""
    require_mem_dir(root)

    # Read raw events
    raw_events = []
    from projectmem.storage import events_path

    path = events_path(root)
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            raw_events.append(json.loads(line))

    # Parse --since
    since_days = None
    if since:
        since = since.strip().lower()
        if since.endswith("d"):
            since_days = int(since[:-1])
        elif since.endswith("w"):
            since_days = int(since[:-1]) * 7
        elif since.endswith("m"):
            since_days = int(since[:-1]) * 30
        else:
            since_days = int(since)

    result = calculate_score(raw_events, since_days=since_days)

    if fmt == "json":
        typer.echo(json.dumps(result, indent=2))
        return
    if fmt == "badge":
        typer.echo(format_badge(result))
        typer.echo(f"\nMarkdown: ![projectmem score]({format_badge(result)})")
        return

    typer.echo(format_terminal(result))
    if verbose:
        typer.echo(_format_verbose_breakdown(raw_events, since_days))


def _format_verbose_breakdown(
    events: list[dict[str, Any]], since_days: int | None
) -> str:
    """Per-component event-level detail surfaced via `pjm score --verbose`.

    Closes L-025a: the bare `--verbose` flag was a no-op. The breakdown
    here lets a user audit *why* the score is what it is — which exact
    events contributed to each component.
    """
    from datetime import datetime, timezone, timedelta

    bold = "\033[1m"
    dim = "\033[2m"
    reset = "\033[0m"

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=since_days) if since_days else None

    def _within(e: dict[str, Any]) -> bool:
        if cutoff is None:
            return True
        try:
            ts = datetime.fromisoformat((e.get("timestamp") or "").replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return True
        return ts >= cutoff

    filtered = [e for e in events if _within(e)]

    failed = [e for e in filtered if e.get("type") == "attempt" and e.get("outcome") == "failed"]
    decisions = [e for e in filtered if e.get("type") == "decision"]
    fixes = [e for e in filtered if e.get("type") == "fix"]

    files_with_gotchas: set[str] = set()
    for e in filtered:
        for f in e.get("files") or []:
            files_with_gotchas.add(f)
        loc = e.get("location") or ""
        loc_file = location_to_file(loc)
        if loc_file:
            files_with_gotchas.add(loc_file)

    lines = ["", f"  {bold}Verbose Breakdown{reset}", f"  {dim}{'─' * 44}{reset}"]
    sections = [
        ("Failed approaches", failed, "summary"),
        ("Decisions documented", decisions, "summary"),
        ("Fixes with context", fixes, "summary"),
    ]
    for title, items, key in sections:
        lines.append(f"  {bold}{title}{reset} ({len(items)})")
        if not items:
            lines.append(f"    {dim}(none){reset}")
        for e in items[-5:]:
            ts = (e.get("timestamp") or "")[:10]
            text = (e.get(key) or "")[:80]
            lines.append(f"    - {ts} {text}")
        lines.append("")
    lines.append(f"  {bold}Files with logged events{reset} ({len(files_with_gotchas)})")
    sample = sorted(files_with_gotchas)[:10]
    for f in sample:
        lines.append(f"    - {f}")
    if len(files_with_gotchas) > 10:
        lines.append(f"    ... and {len(files_with_gotchas) - 10} more")
    return "\n".join(lines)
