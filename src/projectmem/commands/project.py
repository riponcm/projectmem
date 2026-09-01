"""`pjm project` — manage the local address book of ProjectMem repos.

The registry these commands edit is the same one `pjm init` writes and
`pjm dashboard` reads. Registering a project records where it is; it never
copies memory out of the repo, and removing one leaves `.projectmem/` alone.
"""
from __future__ import annotations

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


@project_app.command("list")
def list_command() -> None:
    """Show every registered project."""
    registry = load_registry()
    if not registry.projects:
        typer.echo("No projects registered yet. Run `pjm init` in a repo, or")
        typer.echo("`pjm project register <path>` to add one that already has memory.")
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
