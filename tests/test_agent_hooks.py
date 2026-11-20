import json

import pytest

from projectmem.commands import agent_hooks

BIN = "/opt/venv/bin/pjm-edit-hook"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_claude_code_install_writes_both_events(tmp_path):
    path = agent_hooks.install("claude-code", tmp_path, binary=BIN)
    assert path == tmp_path / ".claude" / "settings.json"
    hooks = read(path)["hooks"]
    assert hooks["PreToolUse"] == [{
        "matcher": "Edit|Write|MultiEdit",
        "hooks": [{"type": "command", "command": BIN, "timeout": 10}],
    }]
    assert hooks["SessionStart"][0]["matcher"] == "compact"


def test_codex_install_uses_apply_patch_and_post_compact(tmp_path):
    path = agent_hooks.install("codex", tmp_path, binary=BIN)
    assert path == tmp_path / ".codex" / "hooks.json"
    hooks = read(path)["hooks"]
    assert hooks["PreToolUse"][0]["matcher"] == "apply_patch"
    assert "matcher" not in hooks["PostCompact"][0]


def test_install_keeps_other_settings_and_hooks(tmp_path):
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir()
    other = {"type": "command", "command": "/usr/local/bin/lint-guard"}
    path.write_text(json.dumps({
        "model": "opus",
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [other]}]},
    }))
    agent_hooks.install("claude-code", tmp_path, binary=BIN)
    data = read(path)
    assert data["model"] == "opus"
    assert data["hooks"]["PreToolUse"][0] == {"matcher": "Bash", "hooks": [other]}
    assert len(data["hooks"]["PreToolUse"]) == 2


def test_reinstall_replaces_instead_of_duplicating(tmp_path):
    agent_hooks.install("claude-code", tmp_path, binary="/old/venv/bin/pjm-edit-hook")
    path = agent_hooks.install("claude-code", tmp_path, binary=BIN)
    groups = read(path)["hooks"]["PreToolUse"]
    assert len(groups) == 1
    assert groups[0]["hooks"][0]["command"] == BIN


def test_a_path_with_spaces_is_quoted_and_still_recognised(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_hooks.os, "name", "posix")
    spaced = "/Users/me/my tools/bin/pjm-edit-hook"
    path = agent_hooks.install("claude-code", tmp_path, binary=spaced)
    command = read(path)["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert command == "'/Users/me/my tools/bin/pjm-edit-hook'"
    agent_hooks.install("claude-code", tmp_path, binary=spaced)
    assert len(read(path)["hooks"]["PreToolUse"]) == 1


def test_uninstall_removes_only_ours(tmp_path):
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir()
    other = {"type": "command", "command": "/usr/local/bin/lint-guard"}
    path.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Edit|Write|MultiEdit", "hooks": [other]},
    ]}}))
    agent_hooks.install("claude-code", tmp_path, binary=BIN)
    _, changed = agent_hooks.uninstall("claude-code", tmp_path)
    assert changed
    assert read(path)["hooks"] == {"PreToolUse": [
        {"matcher": "Edit|Write|MultiEdit", "hooks": [other]},
    ]}


def test_uninstall_without_our_hook_leaves_the_file_alone(tmp_path):
    path = tmp_path / ".codex" / "hooks.json"
    path.parent.mkdir()
    path.write_text('{"hooks": {}}  ')
    _, changed = agent_hooks.uninstall("codex", tmp_path)
    assert not changed
    assert path.read_text() == '{"hooks": {}}  '


def test_invalid_json_is_never_overwritten(tmp_path):
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir()
    path.write_text("{ not json")
    with pytest.raises(agent_hooks.AgentHookError):
        agent_hooks.install("claude-code", tmp_path, binary=BIN)
    assert path.read_text() == "{ not json"


def test_user_level_install_goes_to_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    path = agent_hooks.install("codex", None, binary=BIN)
    assert path == tmp_path / ".codex" / "hooks.json"


def test_cli_rejects_an_unknown_agent(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from projectmem.cli import app

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["hooks", "install", "--agent", "vim", "--project"])
    assert result.exit_code == 1
    assert not (tmp_path / ".claude").exists()


def test_cli_installs_into_the_project(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from projectmem.cli import app

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agent_hooks, "resolve_hook_binary", lambda: BIN)
    result = CliRunner().invoke(app, ["hooks", "install", "--agent", "claude-code", "--project"])
    assert result.exit_code == 0, result.output
    assert "new Claude Code session" in result.output
    assert (tmp_path / ".claude" / "settings.json").exists()
