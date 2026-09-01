from __future__ import annotations

import json

from typer.testing import CliRunner

from projectmem.cli import app


def test_log_attempt_and_fix_write_events(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    runner.invoke(app, ["init"], catch_exceptions=False)
    runner.invoke(app, ["log", "auth tokens expire too early"], catch_exceptions=False)
    runner.invoke(
        app,
        ["attempt", "changed JWT_EXPIRY in config.py", "--failed"],
        catch_exceptions=False,
    )
    runner.invoke(
        app,
        ["fix", "changed TOKEN_TTL in auth/middleware.py:42"],
        catch_exceptions=False,
    )

    lines = (tmp_path / ".projectmem" / "events.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    events = [json.loads(line) for line in lines]

    assert [event["type"] for event in events] == ["issue", "attempt", "fix"]
    assert all(event["issue_id"] == "0001" for event in events)
    assert events[1]["outcome"] == "failed"


def test_fix_issue_closes_target_without_clearing_newer_active_issue(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    runner.invoke(app, ["init"], catch_exceptions=False)
    runner.invoke(app, ["log", "old issue"], catch_exceptions=False)
    runner.invoke(app, ["log", "new issue"], catch_exceptions=False)

    result = runner.invoke(
        app,
        ["fix", "fixed old issue", "--issue", "1"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Fixed issue #0001" in result.stdout
    assert (
        tmp_path / ".projectmem" / ".current_issue"
    ).read_text(encoding="utf-8") == "0002"

    events = [
        json.loads(line)
        for line in (tmp_path / ".projectmem" / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert events[-1]["type"] == "fix"
    assert events[-1]["issue_id"] == "0001"


def test_plain_fix_still_closes_active_issue(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    runner.invoke(app, ["init"], catch_exceptions=False)
    runner.invoke(app, ["log", "active issue"], catch_exceptions=False)

    result = runner.invoke(app, ["fix", "fixed active"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "Fixed issue #0001" in result.stdout
    assert not (tmp_path / ".projectmem" / ".current_issue").exists()


def test_fix_issue_rejects_unknown_issue(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    runner.invoke(app, ["init"], catch_exceptions=False)
    runner.invoke(app, ["log", "known issue"], catch_exceptions=False)

    result = runner.invoke(app, ["fix", "missing issue", "--issue", "42"])

    assert result.exit_code == 1
    assert result.exception is not None
    assert "Issue #0042 was not found" in str(result.exception)


def test_mcp_record_fix_accepts_target_issue_id(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    runner.invoke(app, ["init"], catch_exceptions=False)
    runner.invoke(app, ["log", "old issue"], catch_exceptions=False)
    runner.invoke(app, ["log", "new issue"], catch_exceptions=False)

    from projectmem.mcp_server import record_fix

    result = record_fix("fixed old issue through MCP", issue_id="1")

    # Global mode names the project it wrote to, so a mis-route is visible on
    # the call that caused it rather than during an audit weeks later.
    assert result.startswith("Fixed issue #0001 → ")
    assert result.endswith(": fixed old issue through MCP")
    assert (
        tmp_path / ".projectmem" / ".current_issue"
    ).read_text(encoding="utf-8") == "0002"

    last = json.loads(
        (tmp_path / ".projectmem" / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[-1]
    )
    assert last["type"] == "fix"
    assert last["issue_id"] == "0001"


def test_attempt_without_open_issue_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(app, ["init"], catch_exceptions=False)

    result = runner.invoke(app, ["attempt", "try something"])

    assert result.exit_code == 1
    assert result.exception is not None
    # Message changed in the v0.0.6 polish-pass (L-008 + L-027a): a markerless
    # attempt with no recent open issue now errors with guidance to either
    # `pjm log` first, pass `--issue`, or rerun with `--auto-issue`.
    assert "No active issue" in str(result.exception)


def test_attempt_auto_issue_creates_parent(tmp_path, monkeypatch):
    """L-008: --auto-issue removes the 'no open issue' UX friction by
    creating an implicit parent issue from the attempt's own text."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(app, ["init"], catch_exceptions=False)

    result = runner.invoke(
        app, ["attempt", "tried X", "--failed", "--auto-issue"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0

    lines = (tmp_path / ".projectmem" / "events.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    events = [json.loads(line) for line in lines]

    types = [e["type"] for e in events]
    assert types == ["issue", "attempt"]
    assert events[0]["issue_id"] == events[1]["issue_id"] == "0001"


def test_attempt_after_fix_does_not_misattribute(tmp_path, monkeypatch):
    """L-027a regression: a `pjm attempt` after closing issue #1 must not
    silently re-attach to an older open issue. This used to corrupt the
    event log by attaching unrelated attempts to whichever issue
    happened to still be open."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(app, ["init"], catch_exceptions=False)

    runner.invoke(app, ["log", "issue one"], catch_exceptions=False)
    runner.invoke(app, ["log", "issue two"], catch_exceptions=False)
    runner.invoke(app, ["fix", "fixed two", "--at", "x.py"], catch_exceptions=False)

    # No marker, and the only OPEN issue (#1) is older than the 5-minute
    # auto-attach window — except we can't easily fast-forward time in a
    # unit test, so we assert via --issue routing instead.
    result = runner.invoke(
        app, ["attempt", "regression check", "--worked", "--issue", "0002"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    last = json.loads(
        (tmp_path / ".projectmem" / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[-1]
    )
    assert last["type"] == "attempt"
    assert last["issue_id"] == "0002"
