from __future__ import annotations

from typer.testing import CliRunner

from projectmem.cli import app


def test_init_creates_projectmem_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    result = runner.invoke(app, ["init"], catch_exceptions=False)

    assert result.exit_code == 0
    assert (tmp_path / ".projectmem").is_dir()
    assert (tmp_path / ".projectmem" / "summary.md").is_file()
    assert (tmp_path / ".projectmem" / "AI_INSTRUCTIONS.md").is_file()
    assert (tmp_path / ".projectmem" / "PROJECT_MAP.md").is_file()
    assert (tmp_path / ".projectmem" / "events.jsonl").is_file()
    assert (tmp_path / ".projectmem" / "issues").is_dir()
    assert (tmp_path / ".projectmem" / "config.toml").is_file()
    assert ".projectmem/events.jsonl" in (tmp_path / ".gitignore").read_text(
        encoding="utf-8"
    )

    summary = (tmp_path / ".projectmem" / "summary.md").read_text(encoding="utf-8")
    instructions = (tmp_path / ".projectmem" / "AI_INSTRUCTIONS.md").read_text(
        encoding="utf-8"
    )
    project_map = (tmp_path / ".projectmem" / "PROJECT_MAP.md").read_text(
        encoding="utf-8"
    )
    assert "Project purpose" in summary
    assert "AI assistants" in summary
    assert "Start of every session" in instructions
    assert "PROJECT_MAP.md" in instructions
    # L-036: instructions now show both MCP and CLI paths for each trigger.
    # CLI surface uses `pjm attempt` (the canonical command since v0.0.4);
    # MCP surface uses `record_attempt`. Either signals the trigger table
    # is intact.
    assert "pjm attempt" in instructions
    assert "record_attempt" in instructions
    assert "Status: not created yet" in project_map
    assert "## Structure" in project_map
    assert "## Relationships" in project_map


def test_instructions_command_prints_ai_protocol(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    runner.invoke(app, ["init"], catch_exceptions=False)
    result = runner.invoke(app, ["instructions"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "projectmem AI Instructions" in result.stdout
    # L-036: trigger table now lists MCP tool + CLI command pairs. CLI is
    # `pjm fix` (canonical command since v0.0.4); MCP is `record_fix`.
    assert "pjm fix" in result.stdout
    assert "record_fix" in result.stdout


def test_map_command_prints_project_map(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    runner.invoke(app, ["init"], catch_exceptions=False)
    result = runner.invoke(app, ["map"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "Project Map" in result.stdout
    assert "Status: not created yet" in result.stdout
