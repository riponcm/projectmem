from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, cast


BrainName = Literal["coding", "personal"]

_IDENTIFIER_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_TAG_RE = _IDENTIFIER_RE
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_TIMESTAMP_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"
)
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
    if not _TIMESTAMP_RE.fullmatch(value):
        raise _validation_error(
            f"{field_name} must use UTC RFC 3339 seconds ending in Z"
        )
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
    resolved_path = path.resolve()
    if path_value != str(resolved_path):
        raise _validation_error(
            f"projects[{index}].path must be an absolute resolved path"
        )
    path = resolved_path

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
    missing = _TOP_LEVEL_FIELDS - set(payload)
    if missing:
        raise _validation_error(
            f"top level is missing fields: {', '.join(sorted(missing))}"
        )
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
    except UnicodeDecodeError as exc:
        raise _validation_error(
            f"{destination} must contain valid UTF-8"
        ) from exc
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


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime(
        _TIMESTAMP_FORMAT
    )


def _registry_with(
    registry: Registry,
    *,
    active_project: str | None | object = ...,
    projects: tuple[ProjectRecord, ...] | None = None,
) -> Registry:
    active = registry.active_project if active_project is ... else active_project
    if active is not None and not isinstance(active, str):
        raise _validation_error("active_project must be a string or null")
    return Registry(
        schema_version=registry.schema_version,
        active_project=active,
        projects=registry.projects if projects is None else projects,
        extras=dict(registry.extras),
    )


def find_project(identifier: str, registry: Registry) -> ProjectRecord | None:
    try:
        normalized = _normalize_identifier(identifier, "identifier")
    except RegistryValidationError:
        return None
    for record in registry.projects:
        if record.id == normalized:
            return record
    for record in registry.projects:
        if record.alias == normalized:
            return record
    return None


def find_project_by_path(
    project_path: str | Path, registry: Registry
) -> ProjectRecord | None:
    key = _path_key(project_path)
    return next(
        (record for record in registry.projects if _path_key(record.path) == key),
        None,
    )


def detect_project(
    project_path: str | Path, registry: Registry
) -> ProjectRecord | None:
    candidate = Path(project_path).expanduser()
    if not candidate.exists():
        return None
    candidate = candidate.resolve()
    if candidate.is_file():
        candidate = candidate.parent
    matches = [
        record
        for record in registry.projects
        if candidate == record.path or record.path in candidate.parents
    ]
    if not matches:
        return None
    return max(matches, key=lambda record: len(record.path.parts))


def register_project(
    project_path: str | Path,
    *,
    alias: str | None = None,
    brain: BrainName = "coding",
    tags: Iterable[str] = (),
    allow_uninitialized: bool = False,
    registry_file: Path | None = None,
) -> ProjectRecord:
    path = Path(project_path).expanduser()
    if not path.exists() or not path.is_dir():
        raise ProjectRegistryError(
            f"Project path must be an existing directory: {path}"
        )
    path = path.resolve()
    if not allow_uninitialized and not (path / ".projectmem").is_dir():
        raise ProjectRegistryError(
            f"Project is not initialized: {path}. Run pjm init from that directory."
        )
    if brain not in ("coding", "personal"):
        raise RegistryValidationError("brain must be coding or personal")

    identifier = _normalize_identifier(
        alias if alias is not None else path.name,
        "alias",
    )
    normalized_tags = tuple(sorted({_normalize_tag(tag) for tag in tags}))
    registry = load_registry(registry_file)
    if find_project_by_path(path, registry) is not None:
        raise DuplicateProjectError(f"Project path is already registered: {path}")

    for record in registry.projects:
        if {identifier} & {record.id, record.alias}:
            raise DuplicateProjectError(
                f"Project ID or alias is already registered: {identifier}"
            )

    now = _utc_now()
    record = ProjectRecord(
        id=identifier,
        alias=identifier,
        path=path,
        default_brain=brain,
        tags=normalized_tags,
        created_at=now,
        updated_at=now,
    )
    save_registry(
        _registry_with(registry, projects=(*registry.projects, record)),
        registry_file,
    )
    return record


def _normalize_project_filters(
    *,
    brain: str | None = None,
    tags: Iterable[str] = (),
) -> tuple[BrainName | None, frozenset[str]]:
    if brain not in (None, "coding", "personal"):
        raise RegistryValidationError("brain must be coding or personal")
    return cast(BrainName | None, brain), frozenset(
        _normalize_tag(tag) for tag in tags
    )


def _filter_projects(
    registry: Registry,
    *,
    brain: BrainName | None,
    required_tags: frozenset[str],
) -> tuple[ProjectRecord, ...]:
    return tuple(
        record
        for record in registry.projects
        if (brain is None or record.default_brain == brain)
        and required_tags.issubset(record.tags)
    )


def list_projects(
    *,
    brain: BrainName | None = None,
    tags: Iterable[str] = (),
    registry_file: Path | None = None,
) -> tuple[ProjectRecord, ...]:
    normalized_brain, required_tags = _normalize_project_filters(
        brain=brain,
        tags=tags,
    )
    return _filter_projects(
        load_registry(registry_file),
        brain=normalized_brain,
        required_tags=required_tags,
    )


def _required_project(identifier: str, registry: Registry) -> ProjectRecord:
    record = find_project(identifier, registry)
    if record is None:
        raise ProjectNotRegisteredError(
            f"Project is not registered: {identifier}. Run pjm project list."
        )
    return record


def _replace_project(
    registry: Registry, replacement: ProjectRecord
) -> Registry:
    projects = tuple(
        replacement if record.id == replacement.id else record
        for record in registry.projects
    )
    return _registry_with(registry, projects=projects)


def set_active_project(
    identifier: str, *, registry_file: Path | None = None
) -> ProjectRecord:
    registry = load_registry(registry_file)
    record = _required_project(identifier, registry)
    if registry.active_project != record.id:
        save_registry(
            _registry_with(registry, active_project=record.id),
            registry_file,
        )
    return record


def clear_active_project(*, registry_file: Path | None = None) -> None:
    registry = load_registry(registry_file)
    if registry.active_project is not None:
        save_registry(
            _registry_with(registry, active_project=None),
            registry_file,
        )


def remove_project(
    identifier: str, *, registry_file: Path | None = None
) -> ProjectRecord:
    registry = load_registry(registry_file)
    record = _required_project(identifier, registry)
    projects = tuple(
        candidate for candidate in registry.projects if candidate.id != record.id
    )
    active = None if registry.active_project == record.id else registry.active_project
    save_registry(
        _registry_with(registry, active_project=active, projects=projects),
        registry_file,
    )
    return record


def set_project_brain(
    identifier: str,
    brain: BrainName,
    *,
    registry_file: Path | None = None,
) -> ProjectRecord:
    if brain not in ("coding", "personal"):
        raise RegistryValidationError("brain must be coding or personal")
    registry = load_registry(registry_file)
    record = _required_project(identifier, registry)
    if record.default_brain == brain:
        return record
    replacement = ProjectRecord(
        id=record.id,
        alias=record.alias,
        path=record.path,
        default_brain=brain,
        tags=record.tags,
        created_at=record.created_at,
        updated_at=_utc_now(),
    )
    save_registry(_replace_project(registry, replacement), registry_file)
    return replacement


def add_project_tag(
    identifier: str,
    tag: str,
    *,
    registry_file: Path | None = None,
) -> ProjectRecord:
    normalized = _normalize_tag(tag)
    registry = load_registry(registry_file)
    record = _required_project(identifier, registry)
    if normalized in record.tags:
        return record
    replacement = ProjectRecord(
        id=record.id,
        alias=record.alias,
        path=record.path,
        default_brain=record.default_brain,
        tags=tuple(sorted((*record.tags, normalized))),
        created_at=record.created_at,
        updated_at=_utc_now(),
    )
    save_registry(_replace_project(registry, replacement), registry_file)
    return replacement


def remove_project_tag(
    identifier: str,
    tag: str,
    *,
    registry_file: Path | None = None,
) -> ProjectRecord:
    normalized = _normalize_tag(tag)
    registry = load_registry(registry_file)
    record = _required_project(identifier, registry)
    if normalized not in record.tags:
        return record
    replacement = ProjectRecord(
        id=record.id,
        alias=record.alias,
        path=record.path,
        default_brain=record.default_brain,
        tags=tuple(existing for existing in record.tags if existing != normalized),
        created_at=record.created_at,
        updated_at=_utc_now(),
    )
    save_registry(_replace_project(registry, replacement), registry_file)
    return replacement
