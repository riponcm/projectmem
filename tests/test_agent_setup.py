from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from projectmem.cli import app
from projectmem.commands import agent_hooks, agent_setup

PY = sys.executable
OURS = {"command": PY, "args": ["-m", "projectmem.mcp_server"]}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(home))
    monkeypatch.setenv("PROJECTMEM_HOME", str(home / ".pjm"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    # Tests run from a throwaway uv env; real installs are not.
    monkeypatch.setattr(agent_setup, "ephemeral_install", lambda: False)
    monkeypatch.setattr(agent_hooks, "resolve_hook_binary", lambda: "/opt/pjm/bin/pjm-edit-hook")
    return home


class FakeCli:
    """Stands in for `claude` / `codex`: records calls, answers from a script."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, answers: dict | None = None):
        self.calls: list[list[str]] = []
        self.answers = answers or {}
        monkeypatch.setattr(agent_setup, "_run", self)

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(argv)
        key = " ".join(argv[1:3])
        code, out = self.answers.get(key, (0, ""))
        return subprocess.CompletedProcess(argv, code, stdout=out, stderr="")


def _cursor(home: Path) -> Path:
    return home / ".cursor" / "mcp.json"


def _write(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")


# ── JSON clients ─────────────────────────────────────────────────────────


def test_creates_the_config_when_there_is_none(home: Path) -> None:
    outcome = agent_setup.setup("cursor")

    assert outcome.ok and outcome.changed
    assert json.loads(_cursor(home).read_text()) == {"mcpServers": {"projectmem": OURS}}


def test_keeps_other_servers_and_settings(home: Path) -> None:
    path = home / ".gemini" / "antigravity" / "mcp_config.json"
    before = {
        "mcpServers": {"github": {"command": "npx", "args": ["gh"], "env": {"TOKEN": "t"}}},
        "preferences": {"theme": "dark", "名前": "ü"},
    }
    _write(path, before)

    assert agent_setup.setup("antigravity").ok

    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["mcpServers"]["github"] == before["mcpServers"]["github"]
    assert after["preferences"] == before["preferences"]
    assert after["mcpServers"]["projectmem"] == OURS
    backup = path.with_name(path.name + ".pjm-bak")
    assert json.loads(backup.read_text(encoding="utf-8")) == before


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_keeps_the_file_mode(home: Path) -> None:
    _write(_cursor(home), {"mcpServers": {}})
    _cursor(home).chmod(0o600)

    agent_setup.setup("cursor")

    assert stat.S_IMODE(_cursor(home).stat().st_mode) == 0o600


def test_second_run_writes_nothing(home: Path) -> None:
    agent_setup.setup("cursor")
    first = _cursor(home).read_bytes()

    outcome = agent_setup.setup("cursor")

    assert outcome.ok and not outcome.changed
    assert "already set up" in outcome.lines[0]
    assert _cursor(home).read_bytes() == first


def test_updates_an_old_install_path_and_keeps_its_extra_keys(home: Path) -> None:
    _write(_cursor(home), {"mcpServers": {"projectmem": {
        "command": "/old/venv/bin/python", "args": ["-m", "projectmem.mcp_server"],
        "env": {"KEEP": "1"},
    }}})

    outcome = agent_setup.setup("cursor")

    assert outcome.changed and "updated" in outcome.lines[0]
    entry = json.loads(_cursor(home).read_text())["mcpServers"]["projectmem"]
    assert entry == {**OURS, "env": {"KEEP": "1"}}


@pytest.mark.parametrize("pin", [
    {"args": ["-m", "projectmem.mcp_server", "--root", "/one/repo"]},
    {"args": ["-m", "projectmem.mcp_server"], "cwd": "/one/repo"},
    {"args": ["-m", "projectmem.mcp_server"], "env": {"PROJECTMEM_ROOT": "/one/repo"}},
])
def test_leaves_a_pinned_entry_alone(home: Path, pin: dict) -> None:
    _write(_cursor(home), {"mcpServers": {"projectmem": {"command": "/x/python", **pin}}})
    before = _cursor(home).read_bytes()

    outcome = agent_setup.setup("cursor")

    assert outcome.ok and not outcome.changed
    assert "pinned to one repo" in outcome.lines[0]
    assert _cursor(home).read_bytes() == before


@pytest.mark.parametrize("entry", [
    {"command": "/x/bin/pjm-mcp", "args": []},
    {"command": "/x/python", "args": ["-m", "projectmem.mcp_server"]},
])
def test_does_not_add_a_second_copy_under_our_name(home: Path, entry: dict) -> None:
    _write(_cursor(home), {"mcpServers": {"pjm": entry}})
    before = _cursor(home).read_bytes()

    outcome = agent_setup.setup("cursor")

    assert "as 'pjm'" in outcome.lines[0]
    assert _cursor(home).read_bytes() == before


@pytest.mark.parametrize("text", ['{"mcpServers": {"a": {}},}', '[1, 2]', '{"mcpServers": []}'])
def test_refuses_a_config_it_cannot_read_and_prints_the_entry(home: Path, text: str) -> None:
    _write(_cursor(home), text)

    outcome = agent_setup.setup("cursor")

    assert not outcome.ok
    assert _cursor(home).read_text() == text
    assert any(json.dumps(PY)[1:-1] in line for line in outcome.lines)


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_writes_through_a_dotfiles_symlink(home: Path, tmp_path: Path) -> None:
    real = tmp_path / "dotfiles" / "mcp.json"
    _write(real, {"mcpServers": {"github": {"command": "npx"}}})
    _cursor(home).parent.mkdir(parents=True)
    _cursor(home).symlink_to(real)

    outcome = agent_setup.setup("cursor")

    assert _cursor(home).is_symlink()
    assert "projectmem" in json.loads(real.read_text())["mcpServers"]
    # The backup sits next to the real file, and the message says where.
    assert real.with_name("mcp.json.pjm-bak").exists()
    assert any(str(real.with_name("mcp.json.pjm-bak")) in line for line in outcome.lines)


def test_claude_desktop_path_per_platform(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = agent_setup.json_config_path("claude-desktop")
    assert path.name == "claude_desktop_config.json"
    if sys.platform == "darwin":
        assert path.parent == home / "Library" / "Application Support" / "Claude"
    elif os.name != "nt":
        assert path.parent == home / ".config" / "Claude"


# ── Claude Code ──────────────────────────────────────────────────────────


def test_claude_code_adds_through_its_own_cli(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_setup, "find_claude", lambda: "/bin/claude")
    cli = FakeCli(monkeypatch)

    outcome = agent_setup.setup("claude-code")

    assert outcome.ok and outcome.changed
    assert cli.calls == [[
        "/bin/claude", "mcp", "add", "--scope", "user", "projectmem", "--",
        PY, "-m", "projectmem.mcp_server",
    ]]
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert set(settings["hooks"]) == {"PreToolUse", "SessionStart"}
    assert not (home / ".claude.json").exists()  # never written by projectmem


def test_claude_code_replaces_an_old_entry(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(home / ".claude.json", {"numStartups": 7, "mcpServers": {
        "projectmem": {"type": "stdio", "command": "/old/python", "args": ["-m", "projectmem.mcp_server"]},
    }})
    monkeypatch.setattr(agent_setup, "find_claude", lambda: "/bin/claude")
    cli = FakeCli(monkeypatch)

    agent_setup.setup("claude-code")

    assert [c[1:3] for c in cli.calls] == [["mcp", "remove"], ["mcp", "add"]]


def test_claude_code_unchanged_runs_nothing(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(home / ".claude.json", {"mcpServers": {"projectmem": {"type": "stdio", **OURS, "env": {}}}})
    monkeypatch.setattr(agent_setup, "find_claude", lambda: "/bin/claude")
    cli = FakeCli(monkeypatch)

    agent_setup.setup("claude-code")  # installs the hook
    outcome = agent_setup.setup("claude-code")

    assert cli.calls == []
    assert outcome.ok and not outcome.changed


def test_claude_code_without_its_cli_prints_the_command(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_setup, "find_claude", lambda: None)
    cli = FakeCli(monkeypatch)

    outcome = agent_setup.setup("claude-code")

    assert not outcome.ok and cli.calls == []
    assert any("claude mcp add --scope user projectmem --" in line for line in outcome.lines)


def test_claude_code_cli_failure_is_reported(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_setup, "find_claude", lambda: "/bin/claude")
    FakeCli(monkeypatch, {"mcp add": (1, "")})

    assert not agent_setup.setup("claude-code").ok


# ── Codex ────────────────────────────────────────────────────────────────


def _codex_row(name: str, command: str, args: list[str]) -> dict:
    return {"name": name, "transport": {"type": "stdio", "command": command, "args": args,
                                        "env": None, "cwd": None}}


def test_codex_adds_through_its_own_cli(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_setup.shutil, "which", lambda name: f"/bin/{name}")
    rows = [_codex_row("github", "npx", ["gh"])]
    cli = FakeCli(monkeypatch, {"mcp list": (0, json.dumps(rows))})

    outcome = agent_setup.setup("codex")

    assert outcome.ok and outcome.changed
    assert cli.calls[-1] == ["/bin/codex", "mcp", "add", "projectmem", "--", PY, "-m", "projectmem.mcp_server"]
    assert (home / ".codex" / "hooks.json").exists()


def test_codex_unchanged_adds_nothing(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_setup.shutil, "which", lambda name: f"/bin/{name}")
    rows = [_codex_row("projectmem", PY, ["-m", "projectmem.mcp_server"])]
    cli = FakeCli(monkeypatch, {"mcp list": (0, json.dumps(rows))})

    agent_setup.setup_codex()

    assert [c[1:3] for c in cli.calls] == [["mcp", "list"]]


def test_codex_without_its_cli_appends_and_keeps_the_rest(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_setup.shutil, "which", lambda name: None)
    config = home / ".codex" / "config.toml"
    _write(config, '# mine\nmodel = "gpt-5"  # keep\n')

    first = agent_setup.setup_codex()
    second = agent_setup.setup_codex()

    text = config.read_text(encoding="utf-8")
    assert text.startswith('# mine\nmodel = "gpt-5"  # keep\n\n[mcp_servers.projectmem]\n')
    assert text.count("[mcp_servers.projectmem]") == 1
    assert first.changed and not second.changed


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib")
def test_codex_block_is_valid_toml_for_windows_paths() -> None:
    import tomllib

    want = {"command": r"C:\Users\Ann Lee\pipx\venvs\projectmem\Scripts\python.exe",
            "args": ["-m", "projectmem.mcp_server"]}
    parsed = tomllib.loads(agent_setup._codex_block(want))

    assert parsed["mcp_servers"]["projectmem"] == want


# ── names and the init command ───────────────────────────────────────────


def test_resolve_accepts_aliases_and_drops_repeats(home: Path) -> None:
    assert agent_setup.resolve(["Claude", "claude-code", "CURSOR"]) == ["claude-code", "cursor"]


def test_resolve_refuses_a_throwaway_install(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_setup, "ephemeral_install", lambda: True)
    with pytest.raises(typer.Exit):
        agent_setup.resolve(["cursor"])


def test_init_with_an_unknown_agent_creates_nothing(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)

    result = CliRunner().invoke(app, ["init", "--agent", "vscode"])

    assert result.exit_code == 1
    assert not (repo / ".projectmem").exists()


def test_init_agent_connects_the_client_instead_of_printing_the_config(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)

    result = CliRunner().invoke(app, [
        "init", "--no-hooks", "--no-watch", "--no-backfill", "--no-claude-md",
        "--agent", "cursor",
    ])

    assert result.exit_code == 0, result.output
    assert (repo / ".projectmem").is_dir()
    assert "MCP server added in ~/.cursor/mcp.json" in result.output
    assert "paste this ONCE" not in result.output
    assert json.loads(_cursor(home).read_text())["mcpServers"]["projectmem"] == OURS
