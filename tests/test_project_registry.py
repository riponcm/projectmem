from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import projectmem.project_registry as project_registry
from projectmem.project_registry import (
    DuplicateProjectError,
    ProjectRecord,
    ProjectNotRegisteredError,
    ProjectRegistryError,
    Registry,
    RegistryValidationError,
    add_project_tag,
    clear_active_project,
    detect_project,
    find_project,
    find_project_by_path,
    list_projects,
    load_registry,
    register_project,
    remove_project,
    remove_project_tag,
    save_registry,
    set_active_project,
    set_project_brain,
)


def test_missing_registry_is_side_effect_free(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path))

    registry = load_registry()

    assert registry.schema_version == 1
    assert registry.active_project is None
    assert registry.projects == ()
    assert registry.extras == {}
    assert not (tmp_path / "projects.json").exists()


def _project_payload(path: Path, **overrides):
    payload = {
        "id": "demo",
        "alias": "demo",
        "path": str(path.resolve()),
        "default_brain": "coding",
        "tags": ["local-first", "python"],
        "created_at": "2026-07-16T12:00:00Z",
        "updated_at": "2026-07-16T12:00:00Z",
    }
    payload.update(overrides)
    return payload


def test_registry_round_trip_preserves_extras_and_missing_legacy_tags(tmp_path):
    registry_file = tmp_path / "projects.json"
    payload = {
        "schema_version": 1,
        "active_project": "demo",
        "projects": [_project_payload(tmp_path / "demo", tags=None)],
        "owner": "mike",
        "settings": {"color": "blue"},
    }
    del payload["projects"][0]["tags"]
    registry_file.write_text(json.dumps(payload), encoding="utf-8")

    registry = load_registry(registry_file)

    assert registry.projects[0].tags == ()
    assert registry.extras == {"owner": "mike", "settings": {"color": "blue"}}

    save_registry(registry, registry_file)

    saved = json.loads(registry_file.read_text(encoding="utf-8"))
    assert saved["owner"] == "mike"
    assert saved["settings"] == {"color": "blue"}
    assert saved["projects"][0]["tags"] == []
    assert registry_file.read_bytes().endswith(b"\n")


@pytest.mark.parametrize(
    "payload",
    [
        "{not json",
        json.dumps({"schema_version": 2, "active_project": None, "projects": []}),
        json.dumps(
            {
                "schema_version": 1,
                "active_project": "missing",
                "projects": [],
            }
        ),
        json.dumps(
            {
                "schema_version": 1,
                "active_project": None,
                "projects": [
                    _project_payload(Path.cwd(), unexpected="value")
                ],
            }
        ),
    ],
)
def test_invalid_registry_is_not_rewritten(tmp_path, payload):
    registry_file = tmp_path / "projects.json"
    registry_file.write_text(payload, encoding="utf-8")
    before = registry_file.read_bytes()

    with pytest.raises(RegistryValidationError):
        load_registry(registry_file)

    assert registry_file.read_bytes() == before


def test_registry_requires_active_project_field(tmp_path):
    registry_file = tmp_path / "projects.json"
    registry_file.write_text(
        json.dumps({"schema_version": 1, "projects": []}),
        encoding="utf-8",
    )
    before = registry_file.read_bytes()

    with pytest.raises(RegistryValidationError, match="active_project"):
        load_registry(registry_file)

    assert registry_file.read_bytes() == before


def test_registry_rejects_noncanonical_timestamp(tmp_path):
    registry_file = tmp_path / "projects.json"
    registry_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "active_project": None,
                "projects": [
                    _project_payload(
                        tmp_path / "demo",
                        created_at="2026-7-1T2:3:4Z",
                    )
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RegistryValidationError, match="created_at"):
        load_registry(registry_file)


def test_registry_rejects_invalid_utf8_without_rewriting(tmp_path):
    registry_file = tmp_path / "projects.json"
    registry_file.write_bytes(b"\xff")
    before = registry_file.read_bytes()

    with pytest.raises(RegistryValidationError, match="UTF-8"):
        load_registry(registry_file)

    assert registry_file.read_bytes() == before


def test_registry_rejects_noncanonical_path_without_rewriting(tmp_path):
    registry_file = tmp_path / "projects.json"
    noncanonical_path = tmp_path / "parent" / ".." / "demo"
    project = _project_payload(tmp_path / "demo")
    project["path"] = str(noncanonical_path)
    registry_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "active_project": None,
                "projects": [project],
            }
        ),
        encoding="utf-8",
    )
    before = registry_file.read_bytes()

    with pytest.raises(
        RegistryValidationError,
        match=r"projects\[0\]\.path",
    ):
        load_registry(registry_file)

    assert registry_file.read_bytes() == before


def test_save_validates_before_creating_temporary_file(tmp_path, monkeypatch):
    registry_file = tmp_path / "projects.json"

    def unexpected_tempfile(*args, **kwargs):
        raise AssertionError("temporary file created before validation")

    monkeypatch.setattr(
        "projectmem.project_registry.tempfile.NamedTemporaryFile",
        unexpected_tempfile,
    )

    with pytest.raises(RegistryValidationError):
        save_registry(
            Registry(schema_version=2, active_project=None, projects=()),
            registry_file,
        )


def test_replace_failure_preserves_previous_registry(tmp_path, monkeypatch):
    registry_file = tmp_path / "projects.json"
    registry_file.write_text(
        '{"schema_version":1,"active_project":null,"projects":[]}\n',
        encoding="utf-8",
    )
    before = registry_file.read_bytes()
    registry = Registry(
        schema_version=1,
        active_project=None,
        projects=(
            ProjectRecord(
                id="demo",
                alias="demo",
                path=(tmp_path / "demo").resolve(),
                default_brain="coding",
                tags=(),
                created_at="2026-07-16T12:00:00Z",
                updated_at="2026-07-16T12:00:00Z",
            ),
        ),
    )

    def fail_replace(self, target):
        raise OSError("replace failed")

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        save_registry(registry, registry_file)

    assert registry_file.read_bytes() == before
    assert list(tmp_path.glob(".projects.json.*.tmp")) == []


def test_write_failure_preserves_previous_registry(tmp_path, monkeypatch):
    registry_file = tmp_path / "projects.json"
    registry_file.write_text(
        '{"schema_version":1,"active_project":null,"projects":[]}\n',
        encoding="utf-8",
    )
    before = registry_file.read_bytes()
    registry = Registry(schema_version=1, active_project=None, projects=())
    real_named_temporary_file = project_registry.tempfile.NamedTemporaryFile

    class FailingWriteHandle:
        def __init__(self, *args, **kwargs):
            self._handle = real_named_temporary_file(*args, **kwargs)

        @property
        def name(self):
            return self._handle.name

        def __enter__(self):
            self._handle.__enter__()
            return self

        def __exit__(self, *args):
            return self._handle.__exit__(*args)

        def write(self, value):
            self._handle.write(value[:1])
            raise OSError("write failed")

        def flush(self):
            self._handle.flush()

        def fileno(self):
            return self._handle.fileno()

    monkeypatch.setattr(
        project_registry.tempfile,
        "NamedTemporaryFile",
        FailingWriteHandle,
    )

    with pytest.raises(OSError, match="write failed"):
        save_registry(registry, registry_file)

    assert registry_file.read_bytes() == before
    assert list(tmp_path.glob(".projects.json.*.tmp")) == []


def test_save_fsyncs_before_replacing_registry(tmp_path, monkeypatch):
    registry_file = tmp_path / "projects.json"
    events = []
    real_replace = Path.replace

    def record_fsync(_file_descriptor):
        events.append("fsync")

    def record_replace(self, target):
        events.append("replace")
        return real_replace(self, target)

    monkeypatch.setattr(project_registry.os, "fsync", record_fsync)
    monkeypatch.setattr(Path, "replace", record_replace)

    save_registry(Registry(1, None, ()), registry_file)

    assert events == ["fsync", "replace"]


def _initialized_repo(tmp_path: Path, name: str = "demo") -> Path:
    repo = tmp_path / name
    (repo / ".projectmem").mkdir(parents=True)
    return repo


def test_register_and_find_by_id_alias_and_path(tmp_path):
    registry_file = tmp_path / "home" / "projects.json"
    repo = _initialized_repo(tmp_path)

    record = register_project(
        repo,
        alias="My-Demo",
        brain="personal",
        tags=(" Python ", "local first", "python"),
        registry_file=registry_file,
    )

    assert record.id == "my-demo"
    assert record.alias == "my-demo"
    assert record.path == repo.resolve()
    assert record.default_brain == "personal"
    assert record.tags == ("local-first", "python")
    registry = load_registry(registry_file)
    assert find_project("MY-DEMO", registry) == record
    assert find_project_by_path(repo, registry) == record


def test_registration_requires_existing_directory_and_initialization(tmp_path):
    registry_file = tmp_path / "projects.json"
    missing = tmp_path / "missing"
    uninitialized = tmp_path / "plain"
    uninitialized.mkdir()

    with pytest.raises(ProjectRegistryError):
        register_project(
            missing, allow_uninitialized=True, registry_file=registry_file
        )
    with pytest.raises(ProjectRegistryError):
        register_project(uninitialized, registry_file=registry_file)

    record = register_project(
        uninitialized,
        allow_uninitialized=True,
        registry_file=registry_file,
    )
    assert record.path == uninitialized.resolve()


def test_duplicate_registration_does_not_mutate_registry(tmp_path):
    registry_file = tmp_path / "projects.json"
    repo = _initialized_repo(tmp_path)
    register_project(repo, registry_file=registry_file)
    before = registry_file.read_bytes()

    with pytest.raises(DuplicateProjectError):
        register_project(repo, alias="other", registry_file=registry_file)

    assert registry_file.read_bytes() == before


def test_registry_rejects_duplicate_id_alias_namespace(tmp_path):
    registry_file = tmp_path / "projects.json"
    registry_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "active_project": None,
                "projects": [
                    _project_payload(
                        tmp_path / "first",
                        id="first",
                        alias="shared",
                    ),
                    _project_payload(
                        tmp_path / "second",
                        id="shared",
                        alias="second",
                    ),
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RegistryValidationError, match="identifier 'shared'"):
        load_registry(registry_file)


@pytest.mark.skipif(os.name != "nt", reason="Windows path identity")
def test_registry_rejects_windows_equivalent_paths(tmp_path):
    registry_file = tmp_path / "projects.json"
    path = (tmp_path / "Demo").resolve()
    equivalent = Path(str(path).swapcase())
    registry_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "active_project": None,
                "projects": [
                    _project_payload(path, id="first", alias="first"),
                    _project_payload(
                        equivalent,
                        id="second",
                        alias="second",
                    ),
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RegistryValidationError, match="registered more than once"):
        load_registry(registry_file)


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": "Demo"},
        {"alias": "demo_alias"},
        {"tags": [""]},
        {"updated_at": "2026-02-30T12:00:00Z"},
    ],
)
def test_registry_rejects_noncanonical_project_fields(
    tmp_path, overrides
):
    registry_file = tmp_path / "projects.json"
    registry_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "active_project": None,
                "projects": [_project_payload(tmp_path / "demo", **overrides)],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RegistryValidationError):
        load_registry(registry_file)


def test_detect_project_selects_deepest_registered_ancestor(tmp_path):
    registry_file = tmp_path / "projects.json"
    parent = _initialized_repo(tmp_path, "parent")
    child = parent / "child"
    (child / ".projectmem").mkdir(parents=True)
    target = child / "src" / "module.py"
    target.parent.mkdir()
    target.write_text("", encoding="utf-8")
    parent_record = register_project(parent, registry_file=registry_file)
    child_record = register_project(child, registry_file=registry_file)
    registry = load_registry(registry_file)
    other = parent / "other"
    other.mkdir()

    assert detect_project(target, registry) == child_record
    assert detect_project(other, registry) == parent_record


def test_active_brain_tag_and_remove_mutations_are_atomic(tmp_path, monkeypatch):
    registry_file = tmp_path / "projects.json"
    repo = _initialized_repo(tmp_path)
    original = register_project(repo, registry_file=registry_file)

    active = set_active_project(original.alias.upper(), registry_file=registry_file)
    assert active.id == original.id
    assert load_registry(registry_file).active_project == original.id

    updated_at = "2099-01-01T00:00:00Z"
    monkeypatch.setattr(project_registry, "_utc_now", lambda: updated_at)
    personal = set_project_brain(
        original.id, "personal", registry_file=registry_file
    )
    assert personal.created_at == original.created_at
    assert personal.updated_at == updated_at

    tagged = add_project_tag(
        original.id, " Local   First ", registry_file=registry_file
    )
    tagged_again = add_project_tag(
        original.id, "local-first", registry_file=registry_file
    )
    assert tagged.tags == ("local-first",)
    assert tagged_again.tags == tagged.tags

    untagged = remove_project_tag(
        original.id, "missing", registry_file=registry_file
    )
    assert untagged.tags == ("local-first",)

    removed = remove_project(original.id, registry_file=registry_file)
    assert removed.id == original.id
    registry = load_registry(registry_file)
    assert registry.projects == ()
    assert registry.active_project is None


def test_list_filters_use_brain_and_tag_and_semantics(tmp_path):
    registry_file = tmp_path / "projects.json"
    first_repo = _initialized_repo(tmp_path, "first")
    second_repo = _initialized_repo(tmp_path, "second")
    first = register_project(
        first_repo,
        brain="coding",
        tags=("python", "local"),
        registry_file=registry_file,
    )
    register_project(
        second_repo,
        brain="personal",
        tags=("python",),
        registry_file=registry_file,
    )

    assert list_projects(
        brain="coding", tags=("python", "local"), registry_file=registry_file
    ) == (first,)
    assert list_projects(
        tags=("python", "missing"), registry_file=registry_file
    ) == ()


@pytest.mark.parametrize(
    ("filters", "message"),
    [
        ({"brain": "invalid"}, "brain"),
        ({"tags": ("bad_tag",)}, "tag"),
    ],
)
def test_list_filters_validate_before_loading_registry(
    tmp_path, filters, message
):
    registry_file = tmp_path / "projects.json"
    registry_file.write_text("{not json", encoding="utf-8")

    with pytest.raises(RegistryValidationError, match=message):
        list_projects(registry_file=registry_file, **filters)


def test_missing_mutation_target_does_not_write(tmp_path):
    registry_file = tmp_path / "projects.json"
    save_registry(Registry(1, None, ()), registry_file)
    before = registry_file.read_bytes()

    with pytest.raises(ProjectNotRegisteredError):
        set_active_project("missing", registry_file=registry_file)

    assert registry_file.read_bytes() == before
    clear_active_project(registry_file=registry_file)
    assert registry_file.read_bytes() == before
