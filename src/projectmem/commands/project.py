"""`pjm project` — manage the local address book of ProjectMem repos.

The registry these commands edit is the same one `pjm init` writes and
`pjm dashboard` reads. Registering a project records where it is; it never
copies memory out of the repo, and removing one leaves `.projectmem/` alone.
"""
from __future__ import annotations

import os
from pathlib import Path

import typer

from projectmem.project_registry import (
    ProjectRecord,
    RegistryError,
    add_tag,
    load_registry,
    register,
    remove_tag,
    set_active,
    set_alias,
    unregister,
)
from projectmem.storage import MEM_DIR

project_app = typer.Typer(
    help="Register projects so one MCP server can serve them all.",
    no_args_is_help=True,
)


def _fail(message: str) -> None:
    typer.secho(f"✗ {message}", fg=typer.colors.RED)
    raise typer.Exit(code=1)


def _line(record: ProjectRecord, active: str | None) -> str:
    mark = "●" if record.id == active else " "
    alias = f"  ({record.alias})" if record.alias else ""
    tags = f"  #{' #'.join(record.tags)}" if record.tags else ""
    missing = "" if (record.path / MEM_DIR).is_dir() else "   ⚠ no memory here"
    return f" {mark} {record.id}{alias}{tags}\n     {record.path}{missing}"


@project_app.command("register")
def register_command(
    path: Path = typer.Argument(Path("."), help="Project folder (default: here)."),
    alias: str = typer.Option(None, "--alias", "-a", help="Short name to call it by."),
) -> None:
    """Add a project to the registry."""
    root = path.expanduser().resolve()
    if not (root / MEM_DIR).is_dir():
        _fail(f"{root} has no {MEM_DIR}/ — run `pjm init` there first.")
    try:
        record = register(root, alias=alias)
    except RegistryError as exc:
        _fail(str(exc))
    typer.secho(f"✓ Registered {record.name}", fg=typer.colors.GREEN)
    typer.echo(f"  {record.path}")
    typer.echo(f"  Use it from any folder:  pjm project use {record.name}")


# Directories that never contain a project and would dominate a scan.
_SKIP = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env",
    "site-packages", "Library", "AppData", ".cache", ".Trash",
    "dist", "build", ".next", "target", "vendor", "Pods",
}


def _find_projects(root: Path, max_depth: int) -> list[Path]:
    """Walk for `.projectmem/` folders.

    Explicit and user-invoked. The registry is still never populated by a
    background crawl — this runs because someone typed the command and named
    the directory, which is the difference that matters.
    """
    found: list[Path] = []
    root = root.expanduser().resolve()
    base_depth = len(root.parts)
    for current, dirnames, _ in os.walk(root):
        here = Path(current)
        if len(here.parts) - base_depth >= max_depth:
            dirnames[:] = []
            continue
        dirnames[:] = [
            d
            for d in dirnames
            # keep .projectmem itself; skip other dotfiles and heavy folders
            if d == MEM_DIR or (d not in _SKIP and not d.startswith("."))
        ]
        if MEM_DIR in dirnames:
            found.append(here)
            dirnames[:] = []  # a project inside a project is not a thing
    return found


@project_app.command("scan")
def scan_command(
    paths: list[Path] = typer.Argument(
        None, help="Where to look. Several are allowed (default: here)."
    ),
    depth: int = typer.Option(4, "--depth", "-d", help="How deep to walk."),
    dry_run: bool = typer.Option(
        False, "--dry-run", "-n", help="Show what would be registered, change nothing."
    ),
) -> None:
    """Find projects that already have memory and add them to the registry.

    Useful after upgrading: the registry only ever recorded projects you ran
    `pjm init` on since it existed (0.2.0), so anything older is missing.

    Takes several locations at once, because projects rarely live in one place —
    on Windows especially, they are spread across drives:

        pjm project scan D:\\ E:\\ %USERPROFILE% --depth 3
    """
    roots = [p.expanduser().resolve() for p in (paths or [Path(".")])]
    for root in roots:
        if not root.is_dir():
            _fail(f"{root} is not a directory.")
    found: list[Path] = []
    for root in roots:
        typer.echo(f"Scanning {root} (depth {depth})…")
        for project in _find_projects(root, depth):
            if project not in found:
                found.append(project)
    if not found:
        typer.echo("No projects with memory found here.")
        return

    known = {r.path for r in load_registry().projects}
    fresh = [p for p in found if p not in known]
    for project in found:
        mark = "  " if project in known else "+ "
        note = "  (already registered)" if project in known else ""
        typer.echo(f"  {mark}{project}{note}")
    if not fresh:
        typer.secho(f"\n✓ All {len(found)} already registered.", fg=typer.colors.GREEN)
        return
    if dry_run:
        typer.echo(f"\n{len(fresh)} would be registered. Re-run without --dry-run.")
        return
    for project in fresh:
        try:
            register(project)
        except RegistryError as exc:
            typer.secho(f"  ✗ {project}: {exc}", fg=typer.colors.RED)
    typer.secho(f"\n✓ Registered {len(fresh)} project(s).", fg=typer.colors.GREEN)
    typer.echo("  One MCP server now reaches them all — `pjm project list`.")


@project_app.command("list")
def list_command() -> None:
    """Show every registered project."""
    registry = load_registry()
    if not registry.projects:
        typer.echo("No projects registered yet.")
        typer.echo("")
        typer.echo("  Already have projects with memory? Find them:")
        typer.echo("    pjm project scan ~/code --dry-run")
        typer.echo("")
        typer.echo("  Otherwise run `pjm init` in a repo, or add one directly:")
        typer.echo("    pjm project register <path>")
        return
    typer.echo(f"{len(registry.projects)} project(s)   ● = active\n")
    for record in registry.projects:
        typer.echo(_line(record, registry.active_project))


@project_app.command("use")
def use_command(
    identifier: str = typer.Argument(
        None, help="id, alias or path. Omit to clear the active project."
    ),
) -> None:
    """Set (or clear) the active project used when a call names none."""
    try:
        record = set_active(identifier)
    except RegistryError as exc:
        _fail(str(exc))
    if record is None:
        typer.secho("✓ Active project cleared", fg=typer.colors.GREEN)
        return
    typer.secho(f"✓ Active project: {record.name}", fg=typer.colors.GREEN)
    typer.echo(f"  {record.path}")


@project_app.command("remove")
def remove_command(identifier: str) -> None:
    """Forget a project. Its repo and memory are left untouched."""
    try:
        record = unregister(identifier)
    except RegistryError as exc:
        _fail(str(exc))
    typer.secho(f"✓ Removed {record.name} from the registry", fg=typer.colors.GREEN)
    typer.echo(f"  {record.path} and its {MEM_DIR}/ are untouched.")


@project_app.command("alias")
def alias_command(identifier: str, alias: str) -> None:
    """Give a project a short name."""
    try:
        record = set_alias(identifier, alias)
    except RegistryError as exc:
        _fail(str(exc))
    typer.secho(f"✓ {record.id} is now also '{record.alias}'", fg=typer.colors.GREEN)


@project_app.command("tag")
def tag_command(
    identifier: str,
    tag: str,
    remove: bool = typer.Option(False, "--remove", "-r", help="Remove the tag."),
) -> None:
    """Tag a project (or remove a tag with --remove)."""
    try:
        record = remove_tag(identifier, tag) if remove else add_tag(identifier, tag)
    except RegistryError as exc:
        _fail(str(exc))
    tags = " ".join(f"#{t}" for t in record.tags) or "(none)"
    typer.secho(f"✓ {record.name}: {tags}", fg=typer.colors.GREEN)
