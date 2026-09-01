"""Registry, resolver, and global MCP routing.

The failure these guard against is not a crash: it is a write that succeeds
against the wrong repository. Most of these tests assert on *where* something
landed, not on whether it worked.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from projectmem import project_registry as reg
from projectmem.cli import app
from projectmem.resolver import ResolutionError, resolve
from projectmem.storage import initialize, registered_projects


def _project(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    initialize(root)
    return root


# ── registry ────────────────────────────────────────────────────────────

def test_legacy_list_is_migrated_in_place(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("PROJECTMEM_HOME", str(home))
    alpha, beta = _project(tmp_path, "alpha"), _project(tmp_path, "beta")
    # exactly what v0.2.0 wrote
    (home / "projects.json").write_text(
        json.dumps([str(alpha), str(beta)], indent=1), encoding="utf-8"
    )

    registry = reg.load_registry()

    assert [r.id for r in registry.projects] == ["alpha", "beta"]
    assert [r.path for r in registry.projects] == [alpha, beta]
    payload = json.loads((home / "projects.json").read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    # the pre-migration file cannot be regenerated, so it is kept
    assert (home / "projects.json.bak").exists()


def test_dashboard_still_reads_the_registry_after_migration(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("PROJECTMEM_HOME", str(home))
    alpha = _project(tmp_path, "alpha")
    gone = tmp_path / "deleted"
    (home / "projects.json").write_text(
        json.dumps([str(alpha), str(gone)]), encoding="utf-8"
    )

    # stale entries are skipped, not pruned — forgetting is an explicit act
    assert registered_projects() == [alpha]
    assert len(reg.load_registry().projects) == 2


def test_ids_do_not_collide_for_same_named_folders(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "home"))
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()

    reg.register(_project(tmp_path / "a", "web"))
    reg.register(_project(tmp_path / "b", "web"))

    ids = [r.id for r in reg.projects()]
    assert ids == ["web", "web-2"]


def test_unknown_project_errors_with_the_known_ones(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "home"))
    reg.register(_project(tmp_path, "alpha"))

    with pytest.raises(reg.RegistryError) as excinfo:
        reg.require("nope")

    assert "alpha" in str(excinfo.value)


# ── resolver ────────────────────────────────────────────────────────────

def test_explicit_selection_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "home"))
    alpha, beta = _project(tmp_path, "alpha"), _project(tmp_path, "beta")
    reg.register(alpha)
    reg.register(beta)
    reg.set_active("alpha")

    assert resolve(explicit="beta").root == beta


def test_active_project_is_used_when_no_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    reg.register(_project(tmp_path, "alpha"))
    beta = _project(tmp_path, "beta")
    reg.register(beta)
    reg.set_active("beta")

    resolution = resolve()

    assert resolution.root == beta
    assert resolution.source == "active"


def test_client_roots_outrank_a_stale_active_project(tmp_path, monkeypatch):
    """Where the developer is now beats a mode they set days ago."""
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "home"))
    alpha, beta = _project(tmp_path, "alpha"), _project(tmp_path, "beta")
    reg.register(alpha)
    reg.register(beta)
    reg.set_active("alpha")

    resolution = resolve(roots=[beta])

    assert resolution.root == beta
    assert resolution.source == "roots"


def test_two_usable_roots_is_an_error_not_a_coin_flip(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "home"))
    alpha, beta = _project(tmp_path, "alpha"), _project(tmp_path, "beta")

    with pytest.raises(ResolutionError) as excinfo:
        resolve(roots=[alpha, beta])

    assert "project=" in str(excinfo.value)


def test_nothing_selected_refuses_rather_than_guessing(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    reg.register(_project(tmp_path, "alpha"))

    with pytest.raises(ResolutionError):
        resolve()


def test_pinned_root_is_a_boundary_not_a_default(tmp_path, monkeypatch):
    """A server started with --root must never write outside that repo."""
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "home"))
    alpha, beta = _project(tmp_path, "alpha"), _project(tmp_path, "beta")
    reg.register(alpha)
    reg.register(beta)

    assert resolve(pinned=alpha).root == alpha
    assert resolve(explicit="alpha", pinned=alpha).root == alpha
    with pytest.raises(ResolutionError) as excinfo:
        resolve(explicit="beta", pinned=alpha)
    # and it fails loudly rather than silently writing to alpha
    assert "pinned" in str(excinfo.value)


# ── CLI ─────────────────────────────────────────────────────────────────

def test_project_cli_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "home"))
    alpha = _project(tmp_path, "alpha")
    runner = CliRunner()

    assert runner.invoke(app, ["project", "register", str(alpha), "--alias", "a"]).exit_code == 0
    assert runner.invoke(app, ["project", "use", "a"]).exit_code == 0
    listing = runner.invoke(app, ["project", "list"])
    assert "alpha" in listing.stdout and "●" in listing.stdout

    removal = runner.invoke(app, ["project", "remove", "a"])
    assert removal.exit_code == 0
    assert reg.projects() == ()
    # removing from the registry never touches the repo
    assert (alpha / ".projectmem").is_dir()


def test_registering_an_uninitialised_folder_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "home"))
    plain = tmp_path / "not-a-project"
    plain.mkdir()

    result = CliRunner().invoke(app, ["project", "register", str(plain)])

    assert result.exit_code == 1
    assert "pjm init" in result.stdout


# ── MCP routing ─────────────────────────────────────────────────────────

def test_one_server_writes_to_the_project_it_was_told(tmp_path, monkeypatch):
    """The whole point of global mode, and its worst failure mode."""
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    alpha, beta = _project(tmp_path, "alpha"), _project(tmp_path, "beta")
    reg.register(alpha)
    reg.register(beta)

    import projectmem.mcp_server as server

    monkeypatch.setattr(server, "_PROJECT_ROOT", None)

    assert "→ alpha" in server.log_issue(summary="alpha bug", project="alpha")
    assert "→ beta" in server.log_issue(summary="beta bug", project="beta")

    alpha_events = (alpha / ".projectmem" / "events.jsonl").read_text(encoding="utf-8")
    beta_events = (beta / ".projectmem" / "events.jsonl").read_text(encoding="utf-8")
    assert "alpha bug" in alpha_events and "beta bug" not in alpha_events
    assert "beta bug" in beta_events and "alpha bug" not in beta_events


def test_write_without_a_project_fails_visibly(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    reg.register(_project(tmp_path, "alpha"))

    import projectmem.mcp_server as server

    monkeypatch.setattr(server, "_PROJECT_ROOT", None)
    result = server.add_note(summary="unrouted")

    # safe_tool turns it into readable text rather than killing the session
    assert "No project selected" in result
    events = (tmp_path / "alpha" / ".projectmem" / "events.jsonl").read_text(encoding="utf-8")
    assert "unrouted" not in events
