"""Register the edit hook (`pjm-edit-hook`) with an agent client.

Claude Code and Codex CLI both run `PreToolUse` hooks with the same contract,
so one entry point serves both; only the config file, the matcher and the
compaction event differ. The entries are recognised by the command's file
name, so a reinstall replaces them (a moved venv, say) instead of adding a
second copy, and uninstall removes only them.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from pathlib import Path

import typer

HOOK_NAME = "pjm-edit-hook"
TIMEOUT_SECONDS = 10

AGENTS: dict[str, dict] = {
    "claude-code": {
        "label": "Claude Code",
        "config": (".claude", "settings.json"),
        "events": {
            "PreToolUse": "Edit|Write|MultiEdit",
            # Compaction drops the lessons from the context, so the hook
            # forgets what this session was shown.
            "SessionStart": "compact",
        },
    },
    "codex": {
        "label": "Codex CLI",
        "config": (".codex", "hooks.json"),
        "events": {"PreToolUse": "apply_patch", "PostCompact": None},
    },
}


class AgentHookError(Exception):
    pass


def resolve_hook_binary() -> str:
    """The absolute path of `pjm-edit-hook`, preferring the running install.

    The path is written into the client's config, so the hook keeps working
    under pipx, `uv tool` or a conda env that is not on the client's PATH.
    """
    if os.name == "nt":
        candidates = [Path(sys.prefix) / "Scripts" / f"{HOOK_NAME}.exe"]
    else:
        candidates = [Path(sys.prefix) / "bin" / HOOK_NAME]
    for guess in candidates:
        if guess.is_file():
            return str(guess)
    found = shutil.which(HOOK_NAME)
    if found:
        return str(Path(found).resolve())
    raise AgentHookError(
        f"{HOOK_NAME} not found next to this Python or on PATH. "
        "Reinstall projectmem (pip install -U projectmem) and try again."
    )


def shell_command(path: str) -> str:
    if os.name == "nt":
        return f'"{path}"' if " " in path else path
    return shlex.quote(path)


def config_path(agent: str, project: Path | None) -> Path:
    base = project if project is not None else Path.home()
    folder, name = AGENTS[agent]["config"]
    return base / folder / name


def is_ours(handler: object) -> bool:
    if not isinstance(handler, dict):
        return False
    command = str(handler.get("command", "")).strip().strip("'\"")
    stem = Path(command.replace("\\", "/")).name
    return stem in (HOOK_NAME, f"{HOOK_NAME}.exe")


def strip_ours(groups: list) -> list:
    """Drop our handlers; drop a group only when it held nothing else."""
    kept = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            kept.append(group)
            continue
        handlers = [h for h in group["hooks"] if not is_ours(h)]
        if len(handlers) == len(group["hooks"]):
            kept.append(group)
        elif handlers:
            kept.append({**group, "hooks": handlers})
    return kept


def read_config(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise AgentHookError(
            f"{path} is not valid JSON ({error.msg}, line {error.lineno}); "
            "left it untouched. Fix it and run the command again."
        ) from error
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
        raise AgentHookError(f"{path} has an unexpected shape; left it untouched.")
    return data


def write_config(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".pjm-tmp")
    temp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def install(agent: str, project: Path | None = None, binary: str | None = None) -> Path:
    path = config_path(agent, project)
    data = read_config(path)
    hooks = data.setdefault("hooks", {})
    handler = {
        "type": "command",
        "command": shell_command(binary or resolve_hook_binary()),
        "timeout": TIMEOUT_SECONDS,
    }
    for event, matcher in AGENTS[agent]["events"].items():
        groups = strip_ours(hooks.get(event, []))
        group: dict = {"hooks": [handler]}
        if matcher is not None:
            group = {"matcher": matcher, **group}
        groups.append(group)
        hooks[event] = groups
    write_config(path, data)
    return path


def uninstall(agent: str, project: Path | None = None) -> tuple[Path, bool]:
    path = config_path(agent, project)
    data = read_config(path)
    hooks = data.get("hooks", {})
    changed = False
    for event in AGENTS[agent]["events"]:
        if event not in hooks:
            continue
        groups = strip_ours(hooks[event])
        if groups != hooks[event]:
            changed = True
        if groups:
            hooks[event] = groups
        else:
            del hooks[event]
    if changed:
        if not hooks:
            data.pop("hooks", None)
        write_config(path, data)
    return path, changed


def run(action: str, agent: str, project: bool = False) -> None:
    key = agent.strip().lower().replace("_", "-")
    if key == "claude":
        key = "claude-code"
    if key not in AGENTS:
        typer.echo(f"Unknown agent: {agent}. Use one of: {', '.join(AGENTS)}.", err=True)
        raise typer.Exit(1)
    label = AGENTS[key]["label"]
    root = Path.cwd() if project else None
    try:
        if action == "install":
            path = install(key, root)
            typer.echo(f"Edit hook installed for {label} in {path}")
            typer.echo(
                "  Before each edit, the agent now sees that file's open issues, "
                "failed attempts and decisions. Files with no lessons add nothing."
            )
            typer.echo(f"  It takes effect in a new {label} session.")
            if key == "codex":
                typer.echo("  Codex asks you to trust a new hook before it runs it.")
        elif action == "uninstall":
            path, changed = uninstall(key, root)
            if changed:
                typer.echo(f"Edit hook removed from {path}")
            else:
                typer.echo(f"No projectmem edit hook in {path}; nothing to remove.")
        else:
            typer.echo(f"Unknown action: {action}", err=True)
            raise typer.Exit(1)
    except AgentHookError as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(1) from error
