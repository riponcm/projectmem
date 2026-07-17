from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import typer

from projectmem.storage import initialize


def run(
    no_hooks: bool = False,
    no_global: bool = False,
    no_watch: bool = False,
    no_backfill: bool = False,
    no_claude_md: bool = False,
    no_stack_detect: bool = False,
    no_mcp_config: bool = False,
    no_structure: bool = False,
    global_tags: str | None = None,
    root: Path | None = None,
) -> None:
    path = initialize(root)
    typer.echo(f"Initialized {path}")

    root_path = root or Path.cwd()

    # Drop a CLAUDE.md bridge so AI clients (Claude Code / Antigravity /
    # Cursor) call our MCP tools instead of re-scanning source. AI clients
    # honor root-level rule files even before the MCP server's own
    # `instructions=` field takes effect (L-004f).
    if not no_claude_md:
        _ensure_claude_md(root_path)

    # Auto-detect the stack and pre-populate PROJECT_MAP.md so Setup Mode
    # has something to refine instead of starting from a blank placeholder.
    # Safe: only writes when the current PROJECT_MAP is still the
    # placeholder, never clobbers AI/human edits.
    if not no_stack_detect:
        _populate_project_map_from_stack(root_path)

    # Build the code-structure cache (recursive files + Python import
    # relationships) so the Project Map view renders the real structure right
    # away. Derived from code into .projectmem/structure.json — a disposable,
    # gitignored cache that never touches memory (events/summary/PROJECT_MAP).
    if not no_structure:
        try:
            from projectmem.structure import write_structure

            _, _sdata = write_structure(root_path)
            _st = _sdata["stats"]
            typer.echo(
                f"  Code structure mapped — {_st['files']} files, "
                f"{_st['relationships']} relationships (see the Project Map)."
            )
        except Exception:
            pass  # structure is a nicety; never let it break init

    # Auto-install git hooks unless opted out
    if not no_hooks:
        hooks_dir = root_path / ".git" / "hooks"
        if hooks_dir.exists():
            from projectmem.commands.hooks import install_hooks

            install_hooks(hooks_dir)
            typer.echo("  Auto-capture active — events will be logged automatically.")
        else:
            typer.echo(
                "  Note: No .git directory found. Run `pjm hooks install` after `git init`."
            )

    # Backfill from recent git history so the dashboard is meaningful immediately
    if not no_backfill:
        _try_auto_backfill(root_path)

    # Inherit global memory unless opted out
    if not no_global:
        _inherit_global_memory(root_path, global_tags)

    # Auto-start the file watcher (guarded — skips in CI / non-interactive / no-watchdog)
    if not no_watch:
        _try_auto_start_watch(root_path)

    # Print a copy-pasteable MCP client config block — removes the
    # "how do I wire this up?" friction that hits every new user.
    if not no_mcp_config:
        _print_mcp_config(root_path)

    typer.echo(
        "\n  Next: Start an AI session to refine PROJECT_MAP.md and capture decisions."
    )


_CLAUDE_MD_BRIDGE_START = "<!-- >>> projectmem bridge >>> -->"
_CLAUDE_MD_BRIDGE_END = "<!-- <<< projectmem bridge <<< -->"


def _claude_md_bridge() -> str:
    """The bridge block written into CLAUDE.md.

    Mirrors the MCP server's `instructions=` field (`mcp_server.py`):
    same session-start trio, same write-side rule. Keep the two in sync
    — divergence between them was the L-031 finding: CLAUDE.md said
    "call get_summary first" while the MCP field said "call
    get_instructions first," confusing AI clients that read both.

    Markers (`>>> projectmem bridge >>>` / `<<< projectmem bridge <<<`) let
    us safely re-emit / replace the block on later `pjm init` runs without
    clobbering the rest of the file.
    """
    return (
        f"{_CLAUDE_MD_BRIDGE_START}\n"
        "## projectmem (MANDATORY)\n\n"
        "This project uses projectmem for persistent memory + workflow rules.\n\n"
        "SESSION START — call these three MCP tools, in this order, BEFORE\n"
        "answering ANY question about this project:\n\n"
        "  1. `get_instructions()` — loads the project's mandatory workflow\n"
        "     rules. Without this you will not know how to log work\n"
        "     correctly, when to use `add_note` vs `add_decision`, or how\n"
        "     the event log is structured.\n"
        "  2. `get_summary()` — loads project content. Do NOT answer from\n"
        "     conversation history or by re-reading package.json / README /\n"
        "     source files.\n"
        "  3. `get_project_map()` — loads structural layout when relevant.\n\n"
        "BEFORE modifying ANY file:\n"
        "  - Call `precheck_file(path)` — check failure history first.\n\n"
        "DURING work — use MCP write tools, NEVER edit `.projectmem/`\n"
        "files directly via filesystem write:\n"
        "  - On a bug discovery → `log_issue(summary, location)`.\n"
        "  - After each fix attempt → `record_attempt(summary, outcome)`.\n"
        "  - After confirmation → `record_fix(summary)`.\n"
        "  - On a design choice → `add_decision(summary)`.\n"
        "  - On a gotcha / setup detail → `add_note(summary)`.\n\n"
        "Editing `.projectmem/summary.md` or `.projectmem/PROJECT_MAP.md`\n"
        "directly bypasses event logging and breaks audit replay. The\n"
        "summary file regenerates from `events.jsonl` automatically — write\n"
        "via the MCP tools and the summary will follow.\n\n"
        "Do not re-scan source files when MCP tools can give you the same\n"
        "answer in ~500 tokens instead of ~5000. This is not optional.\n"
        f"{_CLAUDE_MD_BRIDGE_END}\n"
    )


def _ensure_claude_md(root: Path) -> None:
    """Create or safely-update CLAUDE.md with the projectmem bridge block."""
    claude_md = root / "CLAUDE.md"
    bridge = _claude_md_bridge()
    if claude_md.exists():
        content = claude_md.read_text(encoding="utf-8")
        if _CLAUDE_MD_BRIDGE_START in content and _CLAUDE_MD_BRIDGE_END in content:
            # Replace existing bridge in-place.
            start = content.index(_CLAUDE_MD_BRIDGE_START)
            end = content.index(_CLAUDE_MD_BRIDGE_END) + len(_CLAUDE_MD_BRIDGE_END)
            new_content = content[:start] + bridge.rstrip() + content[end:]
            if new_content == content:
                return
            claude_md.write_text(new_content, encoding="utf-8")
            typer.echo("  CLAUDE.md: projectmem bridge refreshed.")
            return
        # Append, preserving the user's existing content.
        new_content = content.rstrip("\n") + "\n\n" + bridge
        claude_md.write_text(new_content, encoding="utf-8")
        typer.echo("  CLAUDE.md: projectmem bridge appended.")
        return
    claude_md.write_text("# CLAUDE.md\n\n" + bridge, encoding="utf-8")
    typer.echo("  CLAUDE.md: created with projectmem bridge.")


def _try_auto_backfill(root: Path) -> None:
    """Backfill recent git history into events.jsonl.

    Safe in all cases:
      - Fresh project (no commits) → silent no-op
      - Existing project → ingests last 20 commits, dedup'd against existing events
      - Not a git repo → silent skip
    """
    # Only run if we're in a git repo
    if not (root / ".git").exists():
        return

    try:
        from projectmem.commands.backfill import run as backfill_run
        # Capture stdout + stderr; we'll print our own one-liner.
        import io, contextlib
        out_buf, err_buf = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            backfill_run(limit=20, root=root)
        # Extract success line if present; ignore "no git history" style errors.
        out = out_buf.getvalue().strip().splitlines()
        ingested = next((ln for ln in out if "Backfilled" in ln or "Added" in ln), None)
        if ingested:
            typer.echo(f"  History: {ingested.strip()}")
        # else: fresh repo or already up-to-date — stay quiet
    except Exception:
        # Never let backfill break init
        pass


def _try_auto_start_watch(root: Path) -> None:
    """Start pjm watch --daemon automatically if the environment supports it."""
    # Skip in CI / pipelines
    ci_markers = ("CI", "CONTINUOUS_INTEGRATION", "GITHUB_ACTIONS", "GITLAB_CI",
                  "JENKINS_HOME", "TRAVIS", "CIRCLECI", "BUILDKITE")
    if any(os.environ.get(m) for m in ci_markers):
        return  # silently skip — don't spawn daemons in pipelines

    # Skip if stdout isn't a TTY (scripted / piped)
    if not sys.stdout.isatty():
        return

    # watchdog is a required dependency — start the daemon (handles its own fork-and-detach)
    try:
        from projectmem.commands.watch import _running_pid, _run_as_daemon

        if _running_pid(root) is not None:
            return  # Already running
        _run_as_daemon(root)
    except Exception:
        # Silent fallback — never block init on watcher failure
        pass


def _inherit_global_memory(root: Path, filter_tags: str | None = None) -> None:
    """Detect stack and inject relevant global memory into AI_INSTRUCTIONS.md."""
    from projectmem.global_memory import (
        detect_stack,
        get_relevant_entries,
        build_inherited_instructions,
        global_dir,
    )

    # Check if global memory exists at all
    gdir = global_dir()
    patterns_file = gdir / "patterns.jsonl"
    gotchas_file = gdir / "library_gotchas.jsonl"

    if not patterns_file.exists() and not gotchas_file.exists():
        return  # No global memory yet — skip silently

    # Detect stack
    stack = detect_stack(root)
    if not stack["tags"] and not stack["libraries"]:
        return  # Can't detect stack — skip

    # Parse filter tags
    tag_list = None
    if filter_tags:
        tag_list = [t.strip() for t in filter_tags.split(",")]

    # Get relevant entries
    relevant = get_relevant_entries(stack, filter_tags=tag_list)
    r_patterns = relevant["patterns"]
    r_gotchas = relevant["gotchas"]

    if not r_patterns and not r_gotchas:
        return  # Nothing relevant

    # Build and inject the instructions section
    instructions_section = build_inherited_instructions(relevant)
    if not instructions_section:
        return

    ai_path = root / ".projectmem" / "AI_INSTRUCTIONS.md"
    if ai_path.exists():
        content = ai_path.read_text(encoding="utf-8")

        # Remove old inherited section if present
        marker_start = "## Global Memory — Inherited Knowledge"
        if marker_start in content:
            # Find start and end of section
            start_idx = content.index(marker_start)
            # Find next ## heading or end of file
            rest = content[start_idx + len(marker_start):]
            next_heading = rest.find("\n## ")
            if next_heading >= 0:
                end_idx = start_idx + len(marker_start) + next_heading
            else:
                end_idx = len(content)
            content = content[:start_idx].rstrip("\n") + "\n\n" + content[end_idx:].lstrip("\n")

        # Append the new section before the Rules section if it exists
        rules_marker = "## Rules"
        if rules_marker in content:
            idx = content.index(rules_marker)
            content = content[:idx] + instructions_section + "\n" + content[idx:]
        else:
            content = content.rstrip("\n") + "\n\n" + instructions_section

        ai_path.write_text(content, encoding="utf-8")

    # Report
    tags_str = ", ".join(stack["tags"][:5])
    typer.echo(f"\n  Global memory: Detected stack [{tags_str}]")
    if r_gotchas:
        typer.echo(f"    → {len(r_gotchas)} library gotchas injected into AI_INSTRUCTIONS.md")
    if r_patterns:
        typer.echo(f"    → {len(r_patterns)} patterns injected into AI_INSTRUCTIONS.md")


# ── L-048: pre-populate PROJECT_MAP.md from detected stack ──────────────
#
# Replaces the manual "let the AI scan source files" Setup-Mode step with
# a concrete map built from the project's manifest files. Only writes when
# the current PROJECT_MAP.md is still the unedited placeholder — never
# clobbers content authored by a human or earlier AI session.

_PROJECT_MAP_PLACEHOLDER_MARKER = "Status: not created yet"


def _populate_project_map_from_stack(root: Path) -> None:
    """If PROJECT_MAP.md is still the placeholder, replace it with detected info."""
    project_map = root / ".projectmem" / "PROJECT_MAP.md"
    if not project_map.exists():
        return
    current = project_map.read_text(encoding="utf-8")
    if _PROJECT_MAP_PLACEHOLDER_MARKER not in current:
        return  # User or AI has already edited — never overwrite.

    try:
        from projectmem.global_memory import detect_stack
        stack = detect_stack(root)
    except Exception:
        return

    description = _extract_project_description(root)
    entry_points = _extract_entry_points(root)
    main_folders = _detect_main_folders(root)

    # If we detected literally nothing useful, leave the placeholder so the
    # AI's Setup Mode prompt can still fill it in the old way.
    has_signal = bool(
        stack.get("tags") or stack.get("libraries")
        or description or entry_points or main_folders
    )
    if not has_signal:
        return

    project_name = root.name
    lines: list[str] = [
        f"# Project Map - {project_name}",
        "",
        "Status: auto-detected from project manifests "
        "(an AI session may refine this).",
        "",
    ]
    if description:
        lines.extend(["## Project purpose", description, ""])

    if stack.get("tags") or stack.get("libraries") or stack.get("manifest_files"):
        lines.append("## Stack")
        if stack.get("tags"):
            lines.append(f"- Tags: {', '.join(sorted(stack['tags'])[:10])}")
        if stack.get("frameworks"):
            lines.append(f"- Frameworks: {', '.join(sorted(stack['frameworks']))}")
        if stack.get("libraries"):
            lines.append(f"- Key libraries: {', '.join(sorted(stack['libraries'])[:10])}")
        if stack.get("manifest_files"):
            lines.append(f"- Detected from: {', '.join(stack['manifest_files'])}")
        lines.append("")

    if main_folders:
        lines.append("## Structure")
        for name, desc in main_folders:
            lines.append(f"- `{name}/` — {desc}" if desc else f"- `{name}/`")
        lines.append("")

    if entry_points:
        lines.append("## Entry points")
        for ep in entry_points:
            lines.append(f"- {ep}")
        lines.append("")

    lines.append(
        "_Generated by `pjm init`. Refine via your AI session: add architecture,"
        " relationships, suggested first reads, and anything stack detection_"
    )
    lines.append("_missed._")

    project_map.write_text("\n".join(lines) + "\n", encoding="utf-8")

    detected_summary = []
    if stack.get("frameworks"):
        detected_summary.append(", ".join(sorted(stack["frameworks"])[:3]))
    elif stack.get("tags"):
        detected_summary.append(", ".join(sorted(stack["tags"])[:3]))
    suffix = f" ({detected_summary[0]})" if detected_summary else ""
    typer.echo(f"  PROJECT_MAP.md: pre-populated from project manifests{suffix}.")


def _extract_project_description(root: Path) -> str | None:
    """Pull a one-line description out of pyproject / package.json / Cargo."""
    # pyproject.toml [project] description = "..."
    py = root / "pyproject.toml"
    if py.exists():
        try:
            content = py.read_text(encoding="utf-8")
            m = re.search(
                r'^\s*description\s*=\s*["\'](.+?)["\']',
                content,
                re.MULTILINE,
            )
            if m:
                return m.group(1).strip()
        except Exception:
            pass
    # package.json "description"
    pj = root / "package.json"
    if pj.exists():
        try:
            data = json.loads(pj.read_text(encoding="utf-8"))
            desc = data.get("description")
            if isinstance(desc, str) and desc.strip():
                return desc.strip()
        except Exception:
            pass
    # Cargo.toml [package] description = "..."
    cargo = root / "Cargo.toml"
    if cargo.exists():
        try:
            content = cargo.read_text(encoding="utf-8")
            m = re.search(
                r'^\s*description\s*=\s*["\'](.+?)["\']',
                content,
                re.MULTILINE,
            )
            if m:
                return m.group(1).strip()
        except Exception:
            pass
    # go.mod has no description field — return None.
    return None


def _extract_entry_points(root: Path) -> list[str]:
    """Pull entry-point command lines from pyproject / package.json scripts."""
    entries: list[str] = []
    py = root / "pyproject.toml"
    if py.exists():
        try:
            content = py.read_text(encoding="utf-8")
            in_scripts = False
            for line in content.split("\n"):
                stripped = line.strip()
                if re.match(r"^\[project\.scripts\]\s*$", stripped):
                    in_scripts = True
                    continue
                if in_scripts and stripped.startswith("["):
                    in_scripts = False
                    continue
                if in_scripts and "=" in stripped:
                    m = re.match(
                        r'^\s*([a-zA-Z0-9_-]+)\s*=\s*["\'](.+?)["\']',
                        line,
                    )
                    if m:
                        entries.append(f"`{m.group(1)}` → `{m.group(2)}`")
        except Exception:
            pass
    pj = root / "package.json"
    if pj.exists():
        try:
            data = json.loads(pj.read_text(encoding="utf-8"))
            scripts = data.get("scripts", {})
            for k in ("start", "dev", "build", "test"):
                if k in scripts and isinstance(scripts[k], str):
                    entries.append(f"`npm run {k}` → `{scripts[k]}`")
        except Exception:
            pass
    return entries[:8]  # Cap to avoid runaway lists.


_COMMON_FOLDERS = {
    "src": "package or application code",
    "lib": "library code",
    "app": "application code",
    "tests": "test suite",
    "test": "test suite",
    "docs": "documentation",
    "examples": "examples / demos",
    "scripts": "scripts and utilities",
    "public": "static public assets",
    "assets": "images / fonts / static assets",
    "components": "UI components",
    "pages": "route pages",
}


# Top-level folders that never belong in a project map (deps, caches, VCS,
# build output, virtualenvs). Everything else is real project structure.
_IGNORE_FOLDERS = {
    ".git", ".hg", ".svn", ".projectmem", ".idea", ".vscode",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "__pycache__",
    "node_modules", "venv", "env", "virtualenv", ".tox", ".eggs",
    "build", "dist", "target", ".gradle", "htmlcov", "coverage",
    ".next", ".nuxt", ".cache", "site-packages",
}


def _detect_main_folders(root: Path) -> list[tuple[str, str]]:
    """List the project's REAL top-level folders (skipping deps/caches/build).

    Walks the actual directory instead of matching a fixed name list, so the
    map reflects THIS project's structure (e.g. `core/`, `features/`) rather
    than only ever finding `src/`/`tests/`. Known names keep a friendly
    description; the rest are listed as-is for an AI session to annotate.
    """
    found: list[tuple[str, str]] = []
    try:
        entries = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return found
    for path in entries:
        name = path.name
        if name.startswith(".") or name in _IGNORE_FOLDERS:
            continue
        found.append((name, _COMMON_FOLDERS.get(name, "")))
        if len(found) >= 15:  # keep the map readable on sprawling repos
            break
    return found


# ── L-049: print MCP client config block at end of init ─────────────────
#
# The first thing every new user asks after `pjm init` is "how do I wire
# this into my client?" Printing the exact JSON they need (with the
# absolute Python path baked in to dodge the Claude-Desktop/Cursor PATH
# gotcha documented in the README) removes a whole class of support questions.

def _print_mcp_config(root: Path) -> None:
    """Print a copy-pasteable MCP client config block + client-config paths."""
    py = sys.executable  # Absolute path — subprocesses don't inherit shell PATH.
    bar = "═" * 62
    typer.echo("")
    typer.echo(bar)
    typer.echo("  MCP client configuration — paste this into your client:")
    typer.echo("")
    typer.echo("    {")
    typer.echo('      "mcpServers": {')
    typer.echo('        "projectmem": {')
    typer.echo(f'          "command": "{py}",')
    typer.echo('          "args": [')
    typer.echo('            "-m", "projectmem.mcp_server",')
    typer.echo(f'            "--root", "{root}"')
    typer.echo('          ]')
    typer.echo("        }")
    typer.echo("      }")
    typer.echo("    }")
    typer.echo("")
    typer.echo("  Client config file locations:")
    typer.echo(
        "    Claude Desktop  ~/Library/Application Support/Claude/"
        "claude_desktop_config.json"
    )
    typer.echo("    Cursor          ~/.cursor/mcp.json  (per-project)")
    typer.echo(
        "    Antigravity     ~/.gemini/antigravity/mcp_config.json  "
        "(legacy IDE; v2 path may differ)"
    )
    typer.echo("    Codex (TOML!)   ~/.codex/config.toml")
    typer.echo("")
    typer.echo("  After pasting: fully quit and restart your client (cold start).")
    typer.echo(bar)
