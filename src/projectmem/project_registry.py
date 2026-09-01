"""The local project registry — one address book for every ProjectMem repo.

v0.2.0 shipped a registry as a plain JSON list of paths, written by `pjm init`
and read by `pjm dashboard`. Global MCP mode needs more: stable ids, aliases,
and an active selection, so one server can route a tool call to the right
repository.

This module owns that file. Schema v1 is a record per project; a legacy list is
migrated on first read, in place, with a `.bak` kept beside it. Nothing about a
project is stored here except how to find it — memory itself never leaves the
repo it belongs to.

Design invariants, mirroring `pjm dashboard`:
  - Local only. No network, no telemetry, no cross-project memory.
  - Registration is explicit (`pjm init`, `pjm project register`), never a
    filesystem crawl.
  - Reads are forgiving (a stale entry is skipped), writes are atomic.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_BRAIN = "coding"

# Ids and aliases are lowercase kebab-case: usable as a CLI argument and as an
# MCP tool parameter without quoting.
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class RegistryError(RuntimeError):
    """Raised for operations the caller can fix (unknown id, duplicate alias)."""


@dataclass(frozen=True)
class ProjectRecord:
    id: str
    path: Path
    alias: str | None = None
    # Parked feature (agreed with Mike, 2026-07): the field stays in the schema
    # so bringing personal memory back later needs no second migration; there is
    # deliberately no CLI to change it.
    default_brain: str = DEFAULT_BRAIN
    tags: tuple[str, ...] = ()
    created_at: str = ""
    updated_at: str = ""

    @property
    def name(self) -> str:
        return self.alias or self.id

    def matches(self, identifier: str) -> bool:
        ident = identifier.strip()
        if ident in (self.id, self.alias):
            return True
        # A path is a valid identifier too, so an agent can pass what it knows.
        try:
            return Path(ident).expanduser().resolve() == self.path
        except OSError:
            return False


@dataclass(frozen=True)
class Registry:
    schema_version: int = SCHEMA_VERSION
    active_project: str | None = None
    projects: tuple[ProjectRecord, ...] = ()
    # Anything a newer projectmem wrote that this version doesn't understand is
    # carried through untouched rather than dropped on the next save.
    extras: dict[str, Any] = field(default_factory=dict)

    def find(self, identifier: str | None) -> ProjectRecord | None:
        if not identifier:
            return None
        for record in self.projects:
            if record.matches(identifier):
                return record
        return None

    def active(self) -> ProjectRecord | None:
        return self.find(self.active_project)


def registry_path() -> Path:
    """Location of the registry, honouring $PROJECTMEM_HOME for tests."""
    home = os.environ.get("PROJECTMEM_HOME")
    base = Path(home) if home else (Path.home() / ".projectmem")
    return base / "projects.json"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(value: str) -> str:
    """Folder name -> id. Falls back to `project` for names with no letters."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug if _SLUG_RE.fullmatch(slug) else "project"


def _unique_id(base: str, taken: set[str]) -> str:
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


# ── load / migrate / save ───────────────────────────────────────────────

def _record_from_payload(payload: dict[str, Any]) -> ProjectRecord | None:
    path = payload.get("path")
    ident = payload.get("id")
    if not isinstance(path, str) or not isinstance(ident, str):
        return None
    tags = payload.get("tags")
    return ProjectRecord(
        id=ident,
        path=Path(path),
        alias=payload.get("alias") or None,
        default_brain=payload.get("default_brain") or DEFAULT_BRAIN,
        tags=tuple(t for t in tags if isinstance(t, str)) if isinstance(tags, list) else (),
        created_at=payload.get("created_at") or "",
        updated_at=payload.get("updated_at") or "",
    )


def _migrate_legacy(paths: list[str]) -> Registry:
    """v0.2.0's plain list of paths -> schema v1 records.

    Order is preserved so `pjm dashboard` keeps its card order. The old format
    carried no timestamps, so migration time is the honest answer for both.
    """
    stamp = _now()
    taken: set[str] = set()
    records = []
    for raw in paths:
        if not isinstance(raw, str):
            continue
        path = Path(raw)
        ident = _unique_id(slugify(path.name), taken)
        taken.add(ident)
        records.append(
            ProjectRecord(id=ident, path=path, created_at=stamp, updated_at=stamp)
        )
    return Registry(projects=tuple(records))


def load_registry(path: Path | None = None) -> Registry:
    """Read the registry, migrating the legacy list format on the way.

    Never raises for a damaged file: a registry that cannot be parsed reads as
    empty, because failing here would break `pjm init` for everyone.
    """
    file = path or registry_path()
    try:
        payload = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Registry()

    if isinstance(payload, list):  # v0.2.0
        migrated = _migrate_legacy(payload)
        _backup(file)
        save_registry(migrated, file)
        return migrated

    if not isinstance(payload, dict):
        return Registry()

    raw_projects = payload.get("projects")
    records = []
    if isinstance(raw_projects, list):
        for item in raw_projects:
            if isinstance(item, dict):
                record = _record_from_payload(item)
                if record is not None:
                    records.append(record)
    known = {"schema_version", "active_project", "projects"}
    return Registry(
        schema_version=int(payload.get("schema_version") or SCHEMA_VERSION),
        active_project=payload.get("active_project") or None,
        projects=tuple(records),
        extras={k: v for k, v in payload.items() if k not in known},
    )


def _backup(file: Path) -> None:
    """Keep one copy of the pre-migration file — it cannot be regenerated."""
    try:
        if file.exists():
            file.with_suffix(".json.bak").write_bytes(file.read_bytes())
    except OSError:
        pass


def save_registry(registry: Registry, path: Path | None = None) -> None:
    """Write the registry atomically — a half-written file loses every project."""
    file = path or registry_path()
    payload: dict[str, Any] = dict(registry.extras)
    payload.update(
        {
            "schema_version": SCHEMA_VERSION,
            "active_project": registry.active_project,
            "projects": [
                {
                    "id": r.id,
                    "alias": r.alias,
                    "path": str(r.path),
                    "default_brain": r.default_brain,
                    "tags": list(r.tags),
                    "created_at": r.created_at,
                    "updated_at": r.updated_at,
                }
                for r in registry.projects
            ],
        }
    )
    file.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(file.parent), prefix=".projects-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=1)
            handle.write("\n")
        os.replace(tmp, file)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        raise


# ── operations ──────────────────────────────────────────────────────────

def register(root: Path, alias: str | None = None) -> ProjectRecord:
    """Add a project (idempotent by resolved path). Returns the record."""
    try:
        resolved = root.expanduser().resolve()
    except OSError as exc:
        raise RegistryError(f"Cannot resolve {root}") from exc

    registry = load_registry()
    existing = next((r for r in registry.projects if r.path == resolved), None)
    if existing is not None:
        if alias and alias != existing.alias:
            return set_alias(existing.id, alias)
        return existing

    if alias is not None:
        _require_free_alias(registry, alias)
    ident = _unique_id(slugify(resolved.name), {r.id for r in registry.projects})
    stamp = _now()
    record = ProjectRecord(
        id=ident, path=resolved, alias=alias, created_at=stamp, updated_at=stamp
    )
    save_registry(
        Registry(
            schema_version=registry.schema_version,
            active_project=registry.active_project,
            projects=registry.projects + (record,),
            extras=registry.extras,
        )
    )
    return record


def _require_free_alias(registry: Registry, alias: str, skip_id: str | None = None) -> None:
    if not _SLUG_RE.fullmatch(alias):
        raise RegistryError(
            f"Alias '{alias}' must be lowercase letters, digits and hyphens."
        )
    for record in registry.projects:
        if record.id == skip_id:
            continue
        if alias in (record.id, record.alias):
            raise RegistryError(f"Alias '{alias}' is already used by {record.path}.")


def _replace_record(registry: Registry, record: ProjectRecord, **changes: Any) -> ProjectRecord:
    updated = replace(record, updated_at=_now(), **changes)
    save_registry(
        Registry(
            schema_version=registry.schema_version,
            active_project=registry.active_project,
            projects=tuple(updated if r.id == record.id else r for r in registry.projects),
            extras=registry.extras,
        )
    )
    return updated


def require(identifier: str) -> ProjectRecord:
    """Look up a project or fail with the list of what is registered.

    Explicit selections are authoritative: an unknown id is an error, never a
    silent fall back to some other repository.
    """
    registry = load_registry()
    record = registry.find(identifier)
    if record is not None:
        return record
    known = ", ".join(r.name for r in registry.projects) or "none registered"
    raise RegistryError(f"No project '{identifier}'. Registered: {known}")


def set_alias(identifier: str, alias: str) -> ProjectRecord:
    registry = load_registry()
    record = registry.find(identifier)
    if record is None:
        raise RegistryError(f"No project '{identifier}'.")
    _require_free_alias(registry, alias, skip_id=record.id)
    return _replace_record(registry, record, alias=alias)


def add_tag(identifier: str, tag: str) -> ProjectRecord:
    registry = load_registry()
    record = registry.find(identifier)
    if record is None:
        raise RegistryError(f"No project '{identifier}'.")
    slug = slugify(tag)
    if slug in record.tags:
        return record
    return _replace_record(registry, record, tags=record.tags + (slug,))


def remove_tag(identifier: str, tag: str) -> ProjectRecord:
    registry = load_registry()
    record = registry.find(identifier)
    if record is None:
        raise RegistryError(f"No project '{identifier}'.")
    slug = slugify(tag)
    return _replace_record(registry, record, tags=tuple(t for t in record.tags if t != slug))


def set_active(identifier: str | None) -> ProjectRecord | None:
    """Select (or clear) the active project."""
    registry = load_registry()
    record = None
    if identifier is not None:
        record = registry.find(identifier)
        if record is None:
            known = ", ".join(r.name for r in registry.projects) or "none registered"
            raise RegistryError(f"No project '{identifier}'. Registered: {known}")
    save_registry(
        Registry(
            schema_version=registry.schema_version,
            active_project=record.id if record else None,
            projects=registry.projects,
            extras=registry.extras,
        )
    )
    return record


def unregister(identifier: str) -> ProjectRecord:
    """Forget a project. The repo and its `.projectmem/` are left untouched."""
    registry = load_registry()
    record = registry.find(identifier)
    if record is None:
        raise RegistryError(f"No project '{identifier}'.")
    save_registry(
        Registry(
            schema_version=registry.schema_version,
            active_project=None if registry.active_project == record.id else registry.active_project,
            projects=tuple(r for r in registry.projects if r.id != record.id),
            extras=registry.extras,
        )
    )
    return record


def projects() -> tuple[ProjectRecord, ...]:
    return load_registry().projects
