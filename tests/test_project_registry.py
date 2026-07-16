from __future__ import annotations

import json
from pathlib import Path

import pytest

from projectmem.project_registry import (
    ProjectRecord,
    Registry,
    RegistryValidationError,
    load_registry,
    save_registry,
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
