from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import typer

from projectmem.project_registry import (
    ProjectRecord,
    ProjectRegistryError,
    add_project_tag,
    detect_project,
    list_projects,
    load_registry,
    register_project,
    remove_project,
    remove_project_tag,
    set_active_project,
    set_project_brain,
)


project_app = typer.Typer(
    help="Register and select ProjectMem projects.",
    no_args_is_help=True,
    add_completion=False,
)
tag_app = typer.Typer(
    help="Manage project tags.",
    no_args_is_help=True,
    add_completion=False,
)
project_app.add_typer(tag_app, name="tag")

T = TypeVar("T")


def _domain_call(function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    try:
        return function(*args, **kwargs)
    except ProjectRegistryError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


def _render_record(record: ProjectRecord) -> str:
    tags = ",".join(record.tags)
    return (
        f"{record.alias}  {record.default_brain}  {tags}  "
        f"{record.path}"
    )


@project_app.command("register")
def register_command(
    path: Path,
    alias: str | None = typer.Option(None, "--alias"),
    brain: str = typer.Option("coding", "--brain"),
    tags: list[str] | None = typer.Option(None, "--tag"),
    allow_uninitialized: bool = typer.Option(
        False, "--allow-uninitialized"
    ),
) -> None:
    record = _domain_call(
        register_project,
        path,
        alias=alias,
        brain=brain,
        tags=tags or (),
        allow_uninitialized=allow_uninitialized,
    )
    typer.echo(f"Registered {_render_record(record)}")


@project_app.command("list")
def list_command(
    brain: str | None = typer.Option(None, "--brain"),
    tags: list[str] | None = typer.Option(None, "--tag"),
) -> None:
    records = _domain_call(
        list_projects,
        brain=brain,
        tags=tags or (),
    )
    registry = _domain_call(load_registry)
    typer.echo("ACTIVE  ALIAS  BRAIN  TAGS  PATH")
    for record in records:
        active = "*" if registry.active_project == record.id else ""
        typer.echo(
            f"{active:<6}  {record.alias}  {record.default_brain}  "
            f"{','.join(record.tags)}  {record.path}"
        )


@project_app.command("detect")
def detect_command(
    path: Path = typer.Option(..., "--path"),
) -> None:
    expanded = path.expanduser()
    if not expanded.exists():
        typer.echo(f"Path does not exist: {expanded}", err=True)
        raise typer.Exit(1)
    resolved = expanded.resolve()
    record = _domain_call(detect_project, resolved, _domain_call(load_registry))
    if record is None:
        typer.echo(f'pjm project register "{resolved}"', err=True)
        raise typer.Exit(1)
    typer.echo(_render_record(record))


@project_app.command("use")
def use_command(identifier: str) -> None:
    record = _domain_call(set_active_project, identifier)
    typer.echo(f"Using project {record.id}")


@project_app.command("remove")
def remove_command(identifier: str) -> None:
    record = _domain_call(remove_project, identifier)
    typer.echo(f"Removed project {record.id}")


@project_app.command("set-brain")
def set_brain_command(identifier: str, brain: str) -> None:
    record = _domain_call(set_project_brain, identifier, brain)
    typer.echo(f"Project {record.id} brain: {record.default_brain}")


@tag_app.command("add")
def tag_add_command(identifier: str, tag: str) -> None:
    record = _domain_call(add_project_tag, identifier, tag)
    typer.echo(f"Project {record.id} tags: {','.join(record.tags)}")


@tag_app.command("remove")
def tag_remove_command(identifier: str, tag: str) -> None:
    record = _domain_call(remove_project_tag, identifier, tag)
    typer.echo(f"Project {record.id} tags: {','.join(record.tags)}")


@tag_app.command("list")
def tag_list_command(identifier: str) -> None:
    registry = _domain_call(load_registry)
    from projectmem.project_registry import find_project

    record = find_project(identifier, registry)
    if record is None:
        typer.echo(f"Project is not registered: {identifier}", err=True)
        raise typer.Exit(1)
    for tag in record.tags:
        typer.echo(tag)
