"""One answer to "which project is this call for?".

Global MCP mode runs a single server process against many repositories, so
every tool call has to be routed. This module is the only place that decides,
and it is deliberately side-effect free: it never chdir()s, never initialises a
project, never writes to the registry.

Precedence, highest first:

  1. A root pinned at startup (``--root`` / ``$PROJECTMEM_ROOT``). That is a
     configured boundary: the server serves exactly that repo, and an explicit
     argument naming a different one is an error rather than a redirect.
  2. An explicit project argument on the call (id, alias, or path).
  3. Roots offered by the MCP client, when exactly one resolves.
  4. The active project (``pjm project use``).
  5. Walking up from the working directory, like git.
  6. Nothing — an error naming the registered projects.

Rules 3 and 4 are the reversal of the original draft, and the reason is the
failure this whole layer exists to prevent: a client root is where the
developer is *right now*, while the active project is a mode they set days ago.
When the two disagree, trusting the stale one writes a fix into the wrong
repository — silently, into the audit trail whose only value is being
trustworthy. An explicit per-call selection still beats both.

Ambiguity is never resolved by guessing. Two client roots that both resolve is
an error, not a coin flip.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from projectmem.project_registry import ProjectRecord, load_registry
from projectmem.storage import MEM_DIR, discover_mem_dir


class ResolutionError(RuntimeError):
    """No project could be chosen. The message tells the caller how to fix it."""


@dataclass(frozen=True)
class Resolution:
    root: Path
    source: str  # explicit | pinned | roots | active | cwd
    name: str

    def describe(self) -> str:
        return f"{self.name} ({self.source})"


def _initialised(path: Path) -> bool:
    return (path / MEM_DIR).is_dir()


def _known() -> str:
    names = [r.name for r in load_registry().projects]
    return ", ".join(names) if names else "none registered"


def _from_record(record: ProjectRecord, source: str) -> Resolution:
    if not _initialised(record.path):
        raise ResolutionError(
            f"'{record.name}' is registered at {record.path}, but that folder has "
            f"no {MEM_DIR}/ any more. Run `pjm init` there, or "
            f"`pjm project remove {record.name}`."
        )
    return Resolution(root=record.path, source=source, name=record.name)


def resolve(
    explicit: str | None = None,
    pinned: Path | None = None,
    roots: list[Path] | None = None,
) -> Resolution:
    """Return the project this call belongs to, or raise with what to do."""
    registry = load_registry()

    # 1. A server started with --root is a boundary, not a default. It was
    #    configured for one repository and must never write outside it, so a
    #    pinned root outranks even an explicit argument — and a mismatched
    #    argument is an error, never silently ignored: an agent told "beta"
    #    must not be allowed to believe its write went to beta.
    if pinned is not None:
        if not _initialised(pinned):
            raise ResolutionError(
                f"--root {pinned} has no {MEM_DIR}/ — run `pjm init` there first."
            )
        record = registry.find(str(pinned))
        name = record.name if record else pinned.name
        if explicit:
            wanted = registry.find(explicit)
            same = (wanted is not None and wanted.path == pinned) or (
                explicit in (name, str(pinned))
            )
            if not same:
                raise ResolutionError(
                    f"This server is pinned to '{name}' ({pinned}) and cannot "
                    f"write to '{explicit}'. Start projectmem without --root to "
                    f"serve several projects, or use that project's own server."
                )
        return Resolution(root=pinned, source="pinned", name=name)

    # 2. explicit — authoritative, and a hard error when wrong
    if explicit:
        record = registry.find(explicit)
        if record is not None:
            return _from_record(record, "explicit")
        candidate = Path(explicit).expanduser()
        if candidate.is_dir():
            if not _initialised(candidate):
                raise ResolutionError(
                    f"{candidate} has no {MEM_DIR}/ — run `pjm init` there first."
                )
            return Resolution(
                root=candidate.resolve(), source="explicit", name=candidate.name
            )
        raise ResolutionError(
            f"No project '{explicit}' — not a registered id, alias, or path. "
            f"Registered: {_known()}"
        )

    # 3. client roots — only when exactly one is usable
    usable = []
    for root in roots or []:
        try:
            candidate = Path(root).expanduser().resolve()
        except OSError:
            continue
        if _initialised(candidate):
            usable.append(candidate)
    if len(usable) == 1:
        record = registry.find(str(usable[0]))
        return Resolution(
            root=usable[0],
            source="roots",
            name=record.name if record else usable[0].name,
        )
    if len(usable) > 1:
        names = ", ".join(p.name for p in usable)
        raise ResolutionError(
            f"Your client offered several projectmem projects ({names}). Pass "
            f"project=\"<name>\" on the call so the write lands in the right one."
        )

    # 4. active project
    active = registry.active()
    if active is not None:
        return _from_record(active, "active")

    # 5. working directory, like git
    found = discover_mem_dir()
    if found is not None:
        root = found.parent
        record = registry.find(str(root))
        return Resolution(
            root=root, source="cwd", name=record.name if record else root.name
        )

    # 6. refuse to guess
    raise ResolutionError(
        "No project selected. Pass project=\"<name>\", or run `pjm project use "
        f"<name>` to set an active one. Registered: {_known()}"
    )
