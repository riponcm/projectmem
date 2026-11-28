"""Connect an agent client to projectmem: `pjm init --agent <client>`.

The MCP server goes into the client's user config once, for every project,
as the same entry `pjm init` prints. Claude Code and Codex get it through
their own `mcp add` commands, because those clients own their config files;
Claude Code in particular rewrites `~/.claude.json` while it runs. Cursor,
Antigravity and Claude Desktop have no such command, so the `projectmem` key
is merged into their JSON file and every other key is left as it was. Claude
Code and Codex also get the edit hook (`agent_hooks`).

Nothing is guessed. A config that does not parse, a projectmem entry pinned
to one repo, and a projectmem server saved under another name are reported
and left alone.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import typer

from projectmem.commands import agent_hooks

SERVER_NAME = "projectmem"
SERVER_ARGS = ["-m", "projectmem.mcp_server"]
CLI_TIMEOUT_SECONDS = 60

CLIENTS: dict[str, dict] = {
    "claude-code": {"label": "Claude Code", "restart": "Start a new Claude Code session."},
    "codex": {"label": "Codex CLI", "restart": "Start a new Codex session."},
    "cursor": {"label": "Cursor", "restart": "Fully quit and reopen Cursor."},
    "antigravity": {"label": "Antigravity", "restart": "Fully quit and reopen Antigravity."},
    "claude-desktop": {
        "label": "Claude Desktop",
        "restart": "Fully quit and reopen Claude Desktop.",
    },
}
ALIASES = {"claude": "claude-code"}
HOOK_CLIENTS = tuple(agent_hooks.AGENTS)  # the clients that run the edit hook


@dataclass
class Outcome:
    ok: bool = True
    lines: list[str] = field(default_factory=list)
    changed: bool = False  # a config was written, so the client needs a restart


class SetupError(Exception):
    pass


def normalize(name: str) -> str | None:
    key = name.strip().lower().replace("_", "-").replace(" ", "-")
    key = ALIASES.get(key, key)
    return key if key in CLIENTS else None


def server_entry() -> dict:
    """The stdio entry every client gets: this Python, by absolute path.

    Clients start servers without the user's shell PATH, so a bare `pjm-mcp`
    would not be found under pipx, `uv tool` or conda.
    """
    return {"command": sys.executable, "args": list(SERVER_ARGS)}


def ephemeral_install() -> bool:
    """True when this pjm runs from a throwaway env, e.g. `uvx projectmem`.

    uv deletes those envs on cache cleanup, so a config pointing into one
    breaks later without warning.
    """
    prefix = Path(sys.prefix).as_posix()
    return "/uv/archive-v" in prefix or "/uv/builds-v" in prefix


def show(path: Path) -> str:
    """A path as people write it: ~/.cursor/mcp.json."""
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(path)


# ── what is already configured ──────────────────────────────────────────


def is_projectmem_server(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    command = Path(str(entry.get("command", "")).replace("\\", "/")).name
    args = entry.get("args") or []
    return command in ("pjm-mcp", "pjm-mcp.exe") or (
        isinstance(args, list) and "projectmem.mcp_server" in args
    )


def is_pinned(entry: dict) -> bool:
    """Pinned to one repo with --root, cwd or PROJECTMEM_ROOT, by choice."""
    args = entry.get("args") or []
    env = entry.get("env") or {}
    return (
        (isinstance(args, list) and "--root" in args)
        or bool(entry.get("cwd"))
        or (isinstance(env, dict) and "PROJECTMEM_ROOT" in env)
    )


def existing_state(servers: dict, want: dict) -> tuple[str, str]:
    """('add' | 'update' | 'unchanged' | 'pinned' | 'other', server name)."""
    ours = servers.get(SERVER_NAME)
    if isinstance(ours, dict):
        if ours.get("command") == want["command"] and list(ours.get("args") or []) == want["args"]:
            return "unchanged", SERVER_NAME
        return ("pinned" if is_pinned(ours) else "update"), SERVER_NAME
    for name, entry in servers.items():
        if is_projectmem_server(entry):
            return "other", str(name)
    return "add", SERVER_NAME


def _left_alone(state: str, name: str, where: str) -> Outcome | None:
    if state == "pinned":
        return Outcome(lines=[
            f"MCP server '{name}' in {where} is pinned to one repo (--root, cwd or "
            "PROJECTMEM_ROOT); left it as it is.",
            "  It serves only that repo. To serve every project, remove the pin and "
            "run this command again.",
        ])
    if state == "other":
        return Outcome(lines=[
            f"projectmem is already set up in {where} as '{name}'; left it as it is."
        ])
    return None


# ── JSON clients: Cursor, Antigravity, Claude Desktop ───────────────────


def json_config_path(client: str) -> Path:
    home = Path.home()
    if client == "cursor":
        return home / ".cursor" / "mcp.json"
    if client == "antigravity":
        return home / ".gemini" / "antigravity" / "mcp_config.json"
    if client == "claude-desktop":
        if sys.platform == "darwin":
            return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
        if os.name == "nt":
            # A Microsoft Store install keeps its real config in the package's
            # LocalCache; ordinary processes do not see %APPDATA%\Claude there.
            local = Path(os.environ.get("LOCALAPPDATA") or (home / "AppData" / "Local"))
            store = sorted((local / "Packages").glob("Claude_*"))
            if store:
                return store[0] / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json"
            appdata = Path(os.environ.get("APPDATA") or (home / "AppData" / "Roaming"))
            return appdata / "Claude" / "claude_desktop_config.json"
        xdg = Path(os.environ.get("XDG_CONFIG_HOME") or (home / ".config"))
        return xdg / "Claude" / "claude_desktop_config.json"
    raise ValueError(client)


def read_json_config(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise SetupError(
            f"{show(path)} is not valid JSON ({error.msg}, line {error.lineno}); left it untouched."
        ) from error
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
        raise SetupError(f"{show(path)} has an unexpected shape; left it untouched.")
    return data


def write_json_config(path: Path, data: dict) -> Path | None:
    """Write atomically, keeping the old file as `<name>.pjm-bak`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    temp = path.with_name(path.name + ".pjm-tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if path.exists():
        backup = path.with_name(path.name + ".pjm-bak")
        shutil.copy2(path, backup)
        shutil.copymode(path, temp)
    os.replace(temp, path)
    return backup


def setup_json_client(client: str) -> Outcome:
    shown = json_config_path(client)
    path = shown.resolve() if shown.is_symlink() else shown  # keep dotfile links
    want = server_entry()
    try:
        data = read_json_config(path)
    except SetupError as error:
        return Outcome(ok=False, lines=[f"Error: {error}", *_json_snippet(want)])
    servers = data.get("mcpServers", {})
    state, name = existing_state(servers, want)
    if state == "unchanged":
        return Outcome(lines=[f"MCP server already set up in {show(shown)}"])
    left = _left_alone(state, name, show(shown))
    if left:
        return left
    if state == "update":
        servers[SERVER_NAME] = {**servers[SERVER_NAME], **want}
    else:
        servers[SERVER_NAME] = want
    data["mcpServers"] = servers
    backup = write_json_config(path, data)
    lines = [f"MCP server {'updated' if state == 'update' else 'added'} in {show(shown)}"]
    if backup:
        lines.append(f"  Previous file kept as {backup.name}; other servers untouched.")
    return Outcome(lines=lines, changed=True)


def _json_snippet(want: dict) -> list[str]:
    block = json.dumps({"mcpServers": {SERVER_NAME: want}}, indent=2)
    return ["  Add this entry by hand:", *("    " + line for line in block.splitlines())]


# ── Claude Code and Codex: their own `mcp add` ───────────────────────────


def _cli_line(argv: list[str]) -> str:
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=CLI_TIMEOUT_SECONDS,
        stdin=subprocess.DEVNULL,
    )


def _failure(argv: list[str], result: subprocess.CompletedProcess) -> Outcome:
    detail = (result.stderr or result.stdout).strip().splitlines()
    return Outcome(ok=False, lines=[
        f"Error: `{_cli_line(argv)}` failed" + (f": {detail[-1]}" if detail else "."),
    ])


def claude_user_servers() -> dict:
    """Claude Code's user-scope servers, read from ~/.claude.json (never written)."""
    path = Path.home() / ".claude.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    return servers if isinstance(servers, dict) else {}


def find_claude() -> str | None:
    """`claude` on PATH, else the copy the Claude desktop app ships (macOS).

    Desktop-app users often have no `claude` on PATH, not even in the app's
    own Code tab. The bundled binary is the same official CLI.
    """
    found = shutil.which("claude")
    if found or sys.platform != "darwin":
        return found
    base = Path.home() / "Library" / "Application Support" / "Claude" / "claude-code"
    bundled = [
        p for p in base.glob("*/*/claude.app/Contents/MacOS/claude")
        if os.access(p, os.X_OK)
    ]
    if not bundled:
        return None
    return str(max(bundled, key=lambda p: p.stat().st_mtime))


def setup_claude_code() -> Outcome:
    want = server_entry()
    state, name = existing_state(claude_user_servers(), want)
    if state == "unchanged":
        return Outcome(lines=["MCP server already set up in Claude Code (user scope)"])
    left = _left_alone(state, name, "Claude Code")
    if left:
        return left
    add = ["claude", "mcp", "add", "--scope", "user", SERVER_NAME, "--",
           want["command"], *want["args"]]
    claude = find_claude()
    if claude is None:
        return Outcome(ok=False, lines=[
            "Claude Code's `claude` command was not found, so the MCP server was not added.",
            "  Run this once in a terminal where `claude` works:",
            f"    {_cli_line(add)}",
        ])
    if state == "update":
        remove = [claude, "mcp", "remove", "--scope", "user", SERVER_NAME]
        result = _run(remove)
        if result.returncode != 0:
            return _failure(remove, result)
    result = _run([claude, *add[1:]])
    if result.returncode != 0:
        return _failure(add, result)
    verb = "updated" if state == "update" else "added"
    return Outcome(
        lines=[f"MCP server {verb} in Claude Code (user scope, every project)"], changed=True
    )


def codex_config_path() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")) / "config.toml"


def codex_servers(codex: str) -> dict:
    result = _run([codex, "mcp", "list", "--json"])
    if result.returncode != 0:
        raise SetupError(
            "`codex mcp list` failed"
            + (f": {result.stderr.strip().splitlines()[-1]}" if result.stderr.strip() else ".")
            + f" Check {codex_config_path()} and run this command again."
        )
    try:
        rows = json.loads(result.stdout or "[]")
    except ValueError as error:
        raise SetupError("`codex mcp list --json` printed something unexpected.") from error
    servers = {}
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and isinstance(row.get("transport"), dict):
            servers[str(row.get("name"))] = row["transport"]
    return servers


_CODEX_TABLE = re.compile(r'^\s*\[\s*mcp_servers\.(?:projectmem|"projectmem")\s*\]', re.M)


def _codex_block(want: dict) -> str:
    # JSON string escapes are valid TOML basic strings, Windows paths included.
    args = ", ".join(json.dumps(a) for a in want["args"])
    return f"[mcp_servers.{SERVER_NAME}]\ncommand = {json.dumps(want['command'])}\nargs = [{args}]\n"


def setup_codex() -> Outcome:
    want = server_entry()
    codex = shutil.which("codex")
    path = codex_config_path()
    if codex is None:
        # No codex binary: append our table as text and touch nothing else.
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        if _CODEX_TABLE.search(text):
            return Outcome(lines=[
                f"{show(path)} already has a [mcp_servers.{SERVER_NAME}] entry; left it as it is."
            ])
        path.parent.mkdir(parents=True, exist_ok=True)
        joiner = "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(joiner + _codex_block(want))
        return Outcome(lines=[f"MCP server added to {show(path)}"], changed=True)
    try:
        servers = codex_servers(codex)
    except SetupError as error:
        return Outcome(ok=False, lines=[f"Error: {error}"])
    state, name = existing_state(servers, want)
    if state == "unchanged":
        return Outcome(lines=["MCP server already set up in Codex"])
    left = _left_alone(state, name, "Codex")
    if left:
        return left
    add = [codex, "mcp", "add", SERVER_NAME, "--", want["command"], *want["args"]]
    result = _run(add)  # replaces an existing entry, keeps the rest of the file
    if result.returncode != 0:
        return _failure(["codex", *add[1:]], result)
    verb = "updated" if state == "update" else "added"
    return Outcome(lines=[f"MCP server {verb} in Codex ({show(path)})"], changed=True)


# ── entry point ──────────────────────────────────────────────────────────


def setup(client: str) -> Outcome:
    if client == "claude-code":
        outcome = setup_claude_code()
    elif client == "codex":
        outcome = setup_codex()
    else:
        outcome = setup_json_client(client)
    if client in HOOK_CLIENTS:
        try:
            hook_file = agent_hooks.config_path(client, None)
            before = hook_file.read_bytes() if hook_file.exists() else None
            path = agent_hooks.install(client)
            if path.read_bytes() == before:
                outcome.lines.append(f"Edit hook already installed in {show(path)}")
            else:
                outcome.lines.append(f"Edit hook installed in {show(path)}")
                outcome.changed = True
        except agent_hooks.AgentHookError as error:
            outcome.ok = False
            outcome.lines.append(f"Error: edit hook not installed: {error}")
    return outcome


def resolve(clients: list[str]) -> list[str]:
    """Validate the names before anything is written; exit on a bad one."""
    keys: list[str] = []
    for name in clients:
        key = normalize(name)
        if key is None:
            typer.echo(
                f"Unknown agent: {name}. Use one of: {', '.join(CLIENTS)}. "
                "For other clients, run `pjm init` and paste the config it prints.",
                err=True,
            )
            raise typer.Exit(1)
        if key not in keys:
            keys.append(key)
    if ephemeral_install():
        typer.echo(
            "Error: this pjm runs from a temporary environment (uvx), which uv deletes "
            "later. Install projectmem first, e.g. `pipx install projectmem` or "
            "`uv tool install projectmem`, then run this command again.",
            err=True,
        )
        raise typer.Exit(1)
    return keys


def run(keys: list[str]) -> bool:
    """Set up each client; print what changed. False when any step failed."""
    all_ok = True
    for key in keys:
        label = CLIENTS[key]["label"]
        outcome = setup(key)
        all_ok = all_ok and outcome.ok
        typer.echo("")
        typer.echo(f"  {label}:")
        for line in outcome.lines:
            typer.echo(f"    {line}")
        if outcome.ok and outcome.changed:
            typer.echo(f"    {CLIENTS[key]['restart']}")
    return all_ok
