from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal


BrainName = Literal["coding", "personal"]

_IDENTIFIER_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_TAG_RE = _IDENTIFIER_RE
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_TOP_LEVEL_FIELDS = {"schema_version", "active_project", "projects"}
_PROJECT_FIELDS = {
    "id",
    "alias",
    "path",
    "default_brain",
    "tags",
    "created_at",
    "updated_at",
}
_PROJECT_REQUIRED_FIELDS = _PROJECT_FIELDS - {"tags"}


class ProjectRegistryError(RuntimeError):
    """Base error for project registry operations."""


class RegistryValidationError(ProjectRegistryError):
    """Raised when registry data does not match schema version 1."""


class DuplicateProjectError(ProjectRegistryError):
    """Raised when a registration conflicts with an existing project."""


class ProjectNotRegisteredError(ProjectRegistryError):
    """Raised when a requested project is not registered."""


@dataclass(frozen=True)
class ProjectRecord:
    id: str
    alias: str
    path: Path
    default_brain: BrainName
    tags: tuple[str, ...]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Registry:
    schema_version: int
    active_project: str | None
    projects: tuple[ProjectRecord, ...]
    extras: Mapping[str, object] = field(default_factory=dict)


def registry_path() -> Path:
    home = os.environ.get("PROJECTMEM_HOME")
    base = Path(home).expanduser() if home else Path.home() / ".projectmem"
    return base / "projects.json"


def _validation_error(message: str) -> RegistryValidationError:
    return RegistryValidationError(f"Invalid project registry: {message}")


def _normalize_identifier(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise _validation_error(f"{field_name} must be a string")
    normalized = value.lower()
    if not _IDENTIFIER_RE.fullmatch(normalized):
        raise _validation_error(
            f"{field_name} must match {_IDENTIFIER_RE.pattern}"
        )
    return normalized


def _normalize_tag(value: str) -> str:
    if not isinstance(value, str):
        raise _validation_error("tags must contain only strings")
    normalized = re.sub(r"\s+", "-", value.strip().lower())
    if not _TAG_RE.fullmatch(normalized):
        raise _validation_error(f"tag must match {_TAG_RE.pattern}")
    return normalized


def _path_key(path: str | Path) -> str:
    resolved = Path(path).expanduser().resolve()
    return os.path.normcase(str(resolved))


def _parse_timestamp(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise _validation_error(f"{field_name} must be an RFC 3339 string")
    try:
        datetime.strptime(value, _TIMESTAMP_FORMAT)
    except ValueError as exc:
        raise _validation_error(
            f"{field_name} must use UTC RFC 3339 seconds ending in Z"
        ) from exc
    return value


def _parse_record(value: object, index: int) -> ProjectRecord:
    if not isinstance(value, dict):
        raise _validation_error(f"projects[{index}] must be an object")
    keys = set(value)
    missing = _PROJECT_REQUIRED_FIELDS - keys
    unknown = keys - _PROJECT_FIELDS
    if missing:
        raise _validation_error(
            f"projects[{index}] is missing fields: {', '.join(sorted(missing))}"
        )
    if unknown:
        raise _validation_error(
            f"projects[{index}] has unknown fields: {', '.join(sorted(unknown))}"
        )

    project_id = _normalize_identifier(value["id"], f"projects[{index}].id")
    alias = _normalize_identifier(value["alias"], f"projects[{index}].alias")
    if value["id"] != project_id or value["alias"] != alias:
        raise _validation_error(f"projects[{index}] identifiers must be lowercase")

    path_value = value["path"]
    if not isinstance(path_value, str):
        raise _validation_error(f"projects[{index}].path must be a string")
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        raise _validation_error(f"projects[{index}].path must be absolute")
    path = path.resolve()

    brain = value["default_brain"]
    if brain not in ("coding", "personal"):
        raise _validation_error(
            f"projects[{index}].default_brain must be coding or personal"
        )

    raw_tags = value.get("tags", [])
    if not isinstance(raw_tags, list):
        raise _validation_error(f"projects[{index}].tags must be an array")
    normalized_tags = tuple(sorted({_normalize_tag(tag) for tag in raw_tags}))
    if list(normalized_tags) != raw_tags:
        raise _validation_error(
            f"projects[{index}].tags must be normalized, sorted, and unique"
        )

    return ProjectRecord(
        id=project_id,
        alias=alias,
        path=path,
        default_brain=brain,
        tags=normalized_tags,
        created_at=_parse_timestamp(
            value["created_at"], f"projects[{index}].created_at"
        ),
        updated_at=_parse_timestamp(
            value["updated_at"], f"projects[{index}].updated_at"
        ),
    )


def _validate_uniqueness(projects: Iterable[ProjectRecord]) -> None:
    names: dict[str, int] = {}
    paths: dict[str, int] = {}
    for index, record in enumerate(projects):
        for name in {record.id, record.alias}:
            previous = names.get(name)
            if previous is not None and previous != index:
                raise _validation_error(
                    f"project identifier {name!r} is used by multiple projects"
                )
            names[name] = index
        key = _path_key(record.path)
        if key in paths:
            raise _validation_error(
                f"project path {str(record.path)!r} is registered more than once"
            )
        paths[key] = index


def _registry_from_payload(payload: object) -> Registry:
    if not isinstance(payload, dict):
        raise _validation_error("top level must be an object")
    if payload.get("schema_version") != 1 or type(
        payload.get("schema_version")
    ) is not int:
        raise _validation_error("schema_version must be 1")

    active_project = payload.get("active_project")
    if active_project is not None:
        normalized_active = _normalize_identifier(
            active_project, "active_project"
        )
        if active_project != normalized_active:
            raise _validation_error("active_project must be lowercase")

    raw_projects = payload.get("projects")
    if not isinstance(raw_projects, list):
        raise _validation_error("projects must be an array")
    projects = tuple(
        _parse_record(record, index)
        for index, record in enumerate(raw_projects)
    )
    _validate_uniqueness(projects)
    if active_project is not None and active_project not in {
        record.id for record in projects
    }:
        raise _validation_error(
            f"active_project {active_project!r} does not reference a project ID"
        )

    extras = {
        key: value for key, value in payload.items() if key not in _TOP_LEVEL_FIELDS
    }
    return Registry(
        schema_version=1,
        active_project=active_project,
        projects=projects,
        extras=extras,
    )


def _record_payload(record: ProjectRecord) -> dict[str, object]:
    return {
        "id": record.id,
        "alias": record.alias,
        "path": str(record.path.expanduser().resolve()),
        "default_brain": record.default_brain,
        "tags": list(record.tags),
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def _registry_payload(registry: Registry) -> dict[str, object]:
    if not isinstance(registry, Registry):
        raise _validation_error("save value must be a Registry")
    extras = dict(registry.extras)
    reserved = set(extras) & _TOP_LEVEL_FIELDS
    if reserved:
        raise _validation_error(
            f"extras contain reserved fields: {', '.join(sorted(reserved))}"
        )
    payload: dict[str, object] = {
        **extras,
        "schema_version": registry.schema_version,
        "active_project": registry.active_project,
        "projects": [_record_payload(record) for record in registry.projects],
    }
    validated = _registry_from_payload(payload)
    canonical: dict[str, object] = {
        **dict(validated.extras),
        "schema_version": validated.schema_version,
        "active_project": validated.active_project,
        "projects": [_record_payload(record) for record in validated.projects],
    }
    try:
        json.dumps(canonical)
    except (TypeError, ValueError) as exc:
        raise _validation_error("extras must contain JSON-compatible values") from exc
    return canonical


def load_registry(path: Path | None = None) -> Registry:
    destination = path or registry_path()
    if not destination.exists():
        return Registry(schema_version=1, active_project=None, projects=())
    try:
        payload = json.loads(destination.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise _validation_error(
            f"{destination} contains malformed JSON"
        ) from exc
    return _registry_from_payload(payload)


def save_registry(registry: Registry, path: Path | None = None) -> None:
    destination = path or registry_path()
    payload = _registry_payload(registry)
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(destination)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
