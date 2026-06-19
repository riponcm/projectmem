"""Tests for the 0.1.4 "Accountable Judgment" feature set.

Covers: supersede marking, stale-memory detection, precheck snooze,
`pjm brief`, failed-approach surfacing, and `pjm export --claude-md`.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

from projectmem.cli import app
from projectmem.models import Event, resolve_event_ref, superseded_ids
from projectmem.storage import append_event, read_events


runner = CliRunner()


def _init(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Isolate the machine-wide global store so auto-promotion from test
    # events never touches the real ~/.projectmem.
    monkeypatch.setenv("HOME", str(tmp_path))
    result = runner.invoke(app, ["init"], catch_exceptions=False)
    assert result.exit_code == 0
    return tmp_path


def _event_ids(tmp_path) -> list[str]:
    lines = (tmp_path / ".projectmem" / "events.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    return [json.loads(line)["id"] for line in lines if line.strip()]


def _git(tmp_path, *args):
    subprocess.run(
        ["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True
    )


def _git_repo(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "t@t.test")
    _git(tmp_path, "config", "user.name", "t")


# ── 1. Supersede marking ─────────────────────────────────────────────


def test_supersede_hides_old_decision_from_summary(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    runner.invoke(app, ["decision", "use bcrypt rounds=12"], catch_exceptions=False)
    old_id = _event_ids(tmp_path)[-1]

    result = runner.invoke(
        app,
        ["decision", "switch to argon2", "--supersedes", old_id],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "supersedes" in result.output

    summary = (tmp_path / ".projectmem" / "summary.md").read_text(encoding="utf-8")
    assert "switch to argon2" in summary
    assert "use bcrypt rounds=12" not in summary

    events = read_events(tmp_path)
    assert old_id in superseded_ids(events)
    # Append-only: the old event line is still physically present.
    assert old_id in _event_ids(tmp_path)


def test_supersede_accepts_unique_prefix(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    runner.invoke(app, ["decision", "first decision"], catch_exceptions=False)
    old_id = _event_ids(tmp_path)[-1]
    prefix = old_id.removeprefix("evt_")[:8]

    result = runner.invoke(
        app,
        ["decision", "second decision", "--supersedes", prefix],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    events = read_events(tmp_path)
    assert old_id in superseded_ids(events)


def test_supersede_bad_ref_fails_without_writing(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    before = len(_event_ids(tmp_path))
    result = runner.invoke(
        app, ["decision", "x", "--supersedes", "evt_nope"], catch_exceptions=False
    )
    assert result.exit_code == 1
    assert len(_event_ids(tmp_path)) == before  # nothing half-written


def test_resolve_event_ref_ambiguous_and_missing():
    events = [
        Event(type="note", summary="a", id="evt_aaaa1111"),
        Event(type="note", summary="b", id="evt_aaaa2222"),
    ]
    assert resolve_event_ref(events, "evt_aaaa1111").summary == "a"
    assert resolve_event_ref(events, "aaaa2222").summary == "b"
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_event_ref(events, "aaaa")
    with pytest.raises(ValueError, match="No event matches"):
        resolve_event_ref(events, "zzzz")


def test_search_tags_superseded(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    runner.invoke(app, ["decision", "use bcrypt everywhere"], catch_exceptions=False)
    old_id = _event_ids(tmp_path)[-1]
    runner.invoke(
        app, ["decision", "use argon2", "--supersedes", old_id],
        catch_exceptions=False,
    )
    result = runner.invoke(app, ["search", "bcrypt"], catch_exceptions=False)
    assert "(superseded)" in result.output
    assert old_id in result.output  # ids now visible for --supersedes UX


# ── 2. Stale-memory detection ────────────────────────────────────────


def test_stale_flags_decision_after_commits(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    _git_repo(tmp_path)
    target = tmp_path / "auth.py"
    target.write_text("v0\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")

    # Back-date the decision so subsequent commits clearly postdate it.
    old_ts = (
        datetime.now(timezone.utc) - timedelta(days=2)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    append_event(
        Event(type="decision", summary="auth uses session cookies",
              location="auth.py", timestamp=old_ts),
        tmp_path,
    )

    for i in range(3):
        target.write_text(f"v{i + 1}\n", encoding="utf-8")
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-m", f"change {i + 1}")

    from projectmem.staleness import find_stale_events

    flagged = find_stale_events(read_events(tmp_path), tmp_path)
    assert any(
        item["file"] == "auth.py" and item["commits_since"] >= 3
        for item in flagged
    )


def test_stale_flags_missing_file_and_skips_superseded(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    append_event(
        Event(type="decision", summary="module X is the entry point",
              location="gone.py"),
        tmp_path,
    )
    gone_id = _event_ids(tmp_path)[-1]

    from projectmem.staleness import find_stale_events

    flagged = find_stale_events(read_events(tmp_path), tmp_path)
    assert any(
        item["event"].id == gone_id and item["commits_since"] == -1
        for item in flagged
    )

    # Supersede it — the flag must disappear (already retired, not noise).
    runner.invoke(
        app, ["decision", "entry point moved", "--supersedes", gone_id],
        catch_exceptions=False,
    )
    flagged = find_stale_events(read_events(tmp_path), tmp_path)
    assert not any(item["event"].id == gone_id for item in flagged)


def test_location_file_ignores_non_paths():
    from projectmem.staleness import location_file

    assert location_file(Event(type="note", summary="s", location="src/a.py:42")) == "src/a.py"
    assert location_file(Event(type="note", summary="s", location="deploy pipeline")) is None
    assert location_file(Event(type="note", summary="s")) is None


# ── 3. Precheck snooze ───────────────────────────────────────────────


def test_parse_snooze_duration():
    from projectmem.commands.precheck import parse_snooze_duration

    assert parse_snooze_duration("30m") == timedelta(minutes=30)
    assert parse_snooze_duration("2h") == timedelta(hours=2)
    assert parse_snooze_duration("1d") == timedelta(days=1)
    with pytest.raises(ValueError):
        parse_snooze_duration("soon")
    with pytest.raises(ValueError):
        parse_snooze_duration("0h")


def test_snooze_silences_and_unsnooze_restores(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)

    result = runner.invoke(app, ["precheck", "--snooze", "2h"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "snoozed" in result.output
    assert (tmp_path / ".projectmem" / "precheck.snooze").exists()

    # The snooze itself is audited as an event.
    events = read_events(tmp_path)
    assert any("snoozed" in e.summary for e in events if e.type == "note")

    # While snoozed, precheck announces the silence instead of checking.
    result = runner.invoke(app, ["precheck"], catch_exceptions=False)
    assert "snoozed" in result.output

    result = runner.invoke(app, ["precheck", "--unsnooze"], catch_exceptions=False)
    assert "re-enabled" in result.output
    assert not (tmp_path / ".projectmem" / "precheck.snooze").exists()


def test_expired_snooze_self_cleans(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    from projectmem.commands.precheck import active_snooze

    marker = tmp_path / ".projectmem" / "precheck.snooze"
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    marker.write_text(past, encoding="utf-8")
    assert active_snooze(tmp_path) is None
    assert not marker.exists()


# ── 4. pjm brief ─────────────────────────────────────────────────────


def test_brief_shows_all_sections(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    runner.invoke(app, ["log", "checkout 500 on empty cart"], catch_exceptions=False)
    runner.invoke(
        app,
        ["attempt", "tried debouncing handler in cart.js", "--failed", "--at", "cart.js"],
        catch_exceptions=False,
    )
    runner.invoke(app, ["decision", "use argon2 hashing"], catch_exceptions=False)

    result = runner.invoke(app, ["brief"], catch_exceptions=False)
    assert result.exit_code == 0
    out = result.output
    assert "Active warnings" in out
    assert "cart.js" in out
    assert "Open issues" in out
    assert "checkout 500" in out
    assert "Recent decisions" in out
    assert "argon2" in out
    assert "Score" in out


def test_brief_runs_clean_on_fresh_project(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    result = runner.invoke(app, ["brief"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "none" in result.output  # empty sections degrade gracefully


def test_brief_console_helpers_are_cp1252_safe(monkeypatch):
    from projectmem.commands import brief

    class Cp1252Stdout:
        encoding = "cp1252"

    monkeypatch.setattr(brief.sys, "stdout", Cp1252Stdout())

    assert brief._rule() == "-" * 60
    text = brief._console_safe("⚠ ─ projectmem brief — projectmem 📈")
    text.encode("cp1252")


# ── 5. Failed-approach surfacing ─────────────────────────────────────


def test_precheck_lists_failed_approaches():
    from projectmem.commands.precheck import _analyze_files

    events = [
        Event(type="attempt", summary="tried CSS contain:layout",
              outcome="failed", location="payment.py"),
        Event(type="attempt", summary="debounced the handler",
              outcome="failed", location="payment.py"),
    ]
    warnings = _analyze_files(["payment.py"], events)
    failed = [w for w in warnings if w["type"] == "failed_attempts"]
    assert failed, "expected a failed_attempts warning"
    details = "\n".join(failed[0]["details"])
    assert "What already failed here" in failed[0]["title"]
    assert "tried CSS contain:layout" in details
    assert "debounced the handler" in details


def test_precheck_console_helpers_are_cp1252_safe(monkeypatch):
    from projectmem.commands import precheck

    class Cp1252Stdout:
        encoding = "cp1252"

    monkeypatch.setattr(precheck.sys, "stdout", Cp1252Stdout())

    assert precheck._rule() == "-" * 60
    text = precheck._console_safe("✗ ─ warning ⚠")
    text.encode("cp1252")


def test_precheck_named_files_cli(tmp_path, monkeypatch):
    """`pjm precheck payment.py` — positional files were advertised in the
    module docstring but never wired into the CLI (caught by the 0.1.4
    testing playground)."""
    _init(tmp_path, monkeypatch)
    runner.invoke(app, ["log", "form jumps"], catch_exceptions=False)
    runner.invoke(
        app, ["attempt", "tried contain layout", "--failed", "--at", "payment.py"],
        catch_exceptions=False,
    )
    result = runner.invoke(app, ["precheck", "payment.py"], catch_exceptions=False)
    assert "What already failed here" in result.output
    assert "tried contain layout" in result.output


def test_search_matches_location_field(tmp_path, monkeypatch):
    """`pjm search payment.py` must find events logged with --at payment.py
    (search previously ignored the location field)."""
    _init(tmp_path, monkeypatch)
    runner.invoke(app, ["log", "form jumps"], catch_exceptions=False)
    runner.invoke(
        app, ["attempt", "tried contain layout", "--failed", "--at", "payment.py"],
        catch_exceptions=False,
    )
    result = runner.invoke(
        app, ["search", "payment.py", "--failed-only"], catch_exceptions=False
    )
    assert "tried contain layout" in result.output


def test_search_failed_only(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    runner.invoke(app, ["log", "jump bug"], catch_exceptions=False)
    runner.invoke(
        app, ["attempt", "tried contain layout fix", "--failed"],
        catch_exceptions=False,
    )
    runner.invoke(
        app, ["attempt", "rewrote the carousel", "--worked"],
        catch_exceptions=False,
    )

    result = runner.invoke(
        app, ["search", "carousel", "--failed-only"], catch_exceptions=False
    )
    assert "No matches" in result.output  # worked attempt filtered out

    result = runner.invoke(
        app, ["search", "contain", "--failed-only"], catch_exceptions=False
    )
    assert "tried contain layout fix" in result.output


# ── 6. pjm export --claude-md ────────────────────────────────────────


def test_export_writes_marked_block(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    runner.invoke(app, ["decision", "use pnpm not npm"], catch_exceptions=False)
    runner.invoke(app, ["note", "fastapi BackgroundTasks swallows exceptions"],
                  catch_exceptions=False)
    runner.invoke(app, ["log", "spinner never stops"], catch_exceptions=False)
    runner.invoke(
        app, ["attempt", "tried CSS contain:layout", "--failed", "--at", "ui.css"],
        catch_exceptions=False,
    )

    result = runner.invoke(app, ["export", "--claude-md"], catch_exceptions=False)
    assert result.exit_code == 0

    content = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert "projectmem memory" in content
    assert "use pnpm not npm" in content
    assert "fastapi BackgroundTasks" in content
    assert "Do NOT retry" in content
    assert "tried CSS contain:layout" in content
    # The init-time MCP bridge block must survive untouched.
    assert "projectmem bridge" in content


def test_export_is_idempotent_and_preserves_user_content(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    claude = tmp_path / "CLAUDE.md"
    claude.write_text(
        "# CLAUDE.md\n\nMy own instructions stay.\n", encoding="utf-8"
    )
    runner.invoke(app, ["decision", "first decision"], catch_exceptions=False)
    runner.invoke(app, ["export"], catch_exceptions=False)
    runner.invoke(app, ["decision", "second decision"], catch_exceptions=False)
    runner.invoke(app, ["export"], catch_exceptions=False)

    content = claude.read_text(encoding="utf-8")
    assert "My own instructions stay." in content
    assert content.count("projectmem memory (auto-generated") == 1  # replaced, not stacked
    assert "second decision" in content


def test_export_excludes_superseded_and_flags_stale(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    runner.invoke(app, ["decision", "old way", "--at", "gone.py"], catch_exceptions=False)
    old_id = _event_ids(tmp_path)[-1]
    runner.invoke(app, ["decision", "still cited", "--at", "missing.py"],
                  catch_exceptions=False)
    runner.invoke(
        app, ["decision", "new way", "--supersedes", old_id], catch_exceptions=False
    )

    result = runner.invoke(app, ["export", "--stdout"], catch_exceptions=False)
    out = result.output
    assert "new way" in out
    assert "old way" not in out                 # superseded → excluded
    assert "possibly stale" in out              # missing.py → flagged, not hidden
    assert "still cited" in out


def test_export_cursor_variant(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    runner.invoke(app, ["decision", "a decision"], catch_exceptions=False)
    runner.invoke(app, ["export", "--cursor"], catch_exceptions=False)
    assert (tmp_path / ".cursorrules").exists()
    assert "a decision" in (tmp_path / ".cursorrules").read_text(encoding="utf-8")


# ── 7. Discovery must not mistake the global store for a project ─────


def test_walkup_discovery_skips_global_store(tmp_path, monkeypatch):
    """Regression: running pjm under $HOME with no project used to land on
    ~/.projectmem (the machine-wide global store, which has no config.toml)
    and silently accrete events into it."""
    from projectmem.storage import ProjectMemError, require_mem_dir

    home = tmp_path / "home"
    fake_global = home / ".projectmem"
    (fake_global / "global").mkdir(parents=True)
    (fake_global / "events.jsonl").write_text("", encoding="utf-8")
    (fake_global / "summary.md").write_text("# accreted\n", encoding="utf-8")
    # NOTE: no config.toml — exactly the global store's shape.

    workdir = home / "Documents" / "scratch"
    workdir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workdir)

    with pytest.raises(ProjectMemError):
        require_mem_dir(None)


def test_walkup_discovery_still_finds_real_projects(tmp_path, monkeypatch):
    from projectmem.storage import require_mem_dir

    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(project)
    runner.invoke(app, ["init"], catch_exceptions=False)

    sub = project / "src" / "deep"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    found = require_mem_dir(None)
    assert found == project / ".projectmem"
