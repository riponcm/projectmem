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

def test_a_0_2_x_registry_is_read_without_being_rewritten(tmp_path, monkeypatch):
    """projects.json keeps the format every version understands."""
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
    # still a list — an older projectmem can still read and append to it
    payload = json.loads((home / "projects.json").read_text(encoding="utf-8"))
    assert payload == [str(alpha), str(beta)]
    # ids and aliases live beside it, where an old version cannot reach them
    meta = json.loads((home / "projects.meta.json").read_text(encoding="utf-8"))
    assert set(meta["by_path"]) == {str(alpha), str(beta)}


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
    """With more than one candidate, no selection is an error — never a pick."""
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    reg.register(_project(tmp_path, "alpha"))
    reg.register(_project(tmp_path, "beta"))

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
    reg.register(_project(tmp_path, "beta"))

    import projectmem.mcp_server as server

    monkeypatch.setattr(server, "_PROJECT_ROOT", None)
    result = server.add_note(summary="unrouted")

    # safe_tool turns it into readable text rather than killing the session
    assert "No project selected" in result
    events = (tmp_path / "alpha" / ".projectmem" / "events.jsonl").read_text(encoding="utf-8")
    assert "unrouted" not in events


def test_a_single_registered_project_needs_no_selection(tmp_path, monkeypatch):
    """The common case: one repo, shared config, no `pjm project use`."""
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    alpha = _project(tmp_path, "alpha")
    reg.register(alpha)

    resolution = resolve()

    assert resolution.root == alpha
    assert resolution.source == "only"

    # …and the moment a second project exists, it stops assuming
    reg.register(_project(tmp_path, "beta"))
    with pytest.raises(ResolutionError):
        resolve()


def test_an_older_projectmem_can_only_append_a_path(tmp_path, monkeypatch):
    """The scenario that used to destroy the registry, replayed exactly.

    0.2.x does `[p for p in data if isinstance(p, str)]` and writes the result
    back. Against a list that is harmless — every path is kept and one added.
    Against the dict format it iterated the KEYS and wrote those instead,
    losing every project. Storing the list is what makes this a non-event.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("PROJECTMEM_HOME", str(home))
    # initialize() registers, so create alpha first and beta only after we have
    # captured what an old projectmem would see.
    alpha = _project(tmp_path, "alpha")
    reg.set_alias("alpha", "a")

    old_view = json.loads((home / "projects.json").read_text(encoding="utf-8"))
    assert old_view == [str(alpha)]           # it understands the file

    # …now an old projectmem in another environment registers another project
    beta = tmp_path / "beta"
    beta.mkdir()
    from projectmem.storage import initialize

    initialize(beta)
    old_view = json.loads((home / "projects.json").read_text(encoding="utf-8"))
    old_view = [p for p in old_view if p != str(beta)] + [str(beta)]
    (home / "projects.json").write_text(json.dumps(old_view), encoding="utf-8")

    registry = reg.load_registry()

    assert [r.path for r in registry.projects] == [alpha, beta]
    assert registry.find("a").path == alpha   # our metadata survived intact


def test_a_dev_format_registry_is_converted_and_backed_up(tmp_path, monkeypatch):
    """0.3.0 dev builds wrote records inline; convert them back to a list."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("PROJECTMEM_HOME", str(home))
    alpha = _project(tmp_path, "alpha")
    (home / "projects.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "active_project": "alpha",
                "projects": [{"id": "alpha", "path": str(alpha), "alias": "a"}],
            }
        ),
        encoding="utf-8",
    )

    registry = reg.load_registry()

    assert [r.id for r in registry.projects] == ["alpha"]
    assert registry.active_project == "alpha"
    assert json.loads((home / "projects.json").read_text(encoding="utf-8")) == [
        str(alpha)
    ]
    # the pre-conversion file cannot be regenerated, so it is kept
    assert (home / "projects.json.bak").exists()


def test_windows_paths_survive_migration(tmp_path, monkeypatch):
    """A Windows 0.2.x registry holds C:\\... — not a leading slash in sight.

    The junk-entry filter must reject registry keys without also rejecting
    every path a Windows user has.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("PROJECTMEM_HOME", str(home))
    (home / "projects.json").write_text(
        json.dumps(
            [
                "schema_version",              # junk from a clobber
                r"C:\Users\dev\repos\app",     # ordinary Windows path
                r"\\fileserver\share\proj",    # UNC share
                "relative/path",               # never valid
            ]
        ),
        encoding="utf-8",
    )

    ids = [r.id for r in reg.load_registry().projects]

    # On POSIX only the junk-shaped entries are dropped; the Windows-style ones
    # are not absolute here either, so assert the invariant that holds on both:
    # nothing that is merely a bare word survives.
    assert "schema-version" not in ids
    assert "relative-path" not in ids


def test_absolute_check_is_used_rather_than_a_slash_test():
    """Guard the regression itself.

    The Windows behaviour cannot be exercised from POSIX, so assert on the code:
    a leading-slash test would silently empty every Windows user's registry.
    Comments are stripped first — the one above the check names the very
    pattern it forbids.
    """
    import inspect

    from projectmem import project_registry

    code = "\n".join(
        line.split("#", 1)[0]
        for line in inspect.getsource(project_registry.load_registry).splitlines()
    )
    assert "is_absolute()" in code
    assert 'startswith("/")' not in code


def test_a_utf8_bom_does_not_empty_the_registry(tmp_path, monkeypatch):
    """PowerShell's Out-File, Notepad and Set-Content all write a UTF-8 BOM.

    Read as plain utf-8, three invisible bytes make the file unparseable and
    every project silently disappears — which is exactly what happened on a
    real Windows machine.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("PROJECTMEM_HOME", str(home))
    alpha = _project(tmp_path, "alpha")
    (home / "projects.json").write_bytes(
        b"\xef\xbb\xbf" + json.dumps([str(alpha)]).encode("utf-8")
    )

    assert [r.id for r in reg.load_registry().projects] == ["alpha"]


def test_a_utf8_bom_does_not_break_the_event_log(tmp_path):
    """Same three bytes at the top of events.jsonl used to raise on line 1."""
    from projectmem.models import Event
    from projectmem.storage import append_event, events_path, read_events

    project = _project(tmp_path, "alpha")
    append_event(Event(type="note", summary="first"), root=project)
    path = events_path(project)
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())

    assert [e.summary for e in read_events(project)] == ["first"]


def test_scan_finds_projects_the_registry_never_knew_about(tmp_path, monkeypatch):
    """The upgrade path: the registry only holds what `pjm init` recorded.

    A 0.1.x user has projects with memory and an empty registry, so global mode
    would have nothing to route to until they re-inited everything by hand.
    """
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "home"))
    work = tmp_path / "work"
    work.mkdir()
    (work / "one").mkdir()
    (work / "nested").mkdir()
    (work / "nested" / "two").mkdir()
    initialize(work / "one")
    initialize(work / "nested" / "two")
    # noise that must not be walked into or registered
    (work / "node_modules" / "pkg").mkdir(parents=True)
    (work / "plain").mkdir()
    # initialize() registers, so clear the registry to reproduce the real
    # situation: memory on disk that predates the registry existing.
    (tmp_path / "home" / "projects.json").unlink()
    (tmp_path / "home" / "projects.meta.json").unlink()
    assert reg.projects() == ()

    runner = CliRunner()
    preview = runner.invoke(app, ["project", "scan", str(work), "--dry-run"])

    assert preview.exit_code == 0
    assert "would be registered" in preview.stdout
    assert reg.projects() == ()  # dry run changed nothing

    result = runner.invoke(app, ["project", "scan", str(work)])

    assert result.exit_code == 0
    registered = {r.path for r in reg.projects()}
    assert registered == {work / "one", work / "nested" / "two"}


def test_scan_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "home"))
    work = tmp_path / "work"
    work.mkdir()
    (work / "one").mkdir()
    initialize(work / "one")
    (tmp_path / "home" / "projects.json").unlink()
    (tmp_path / "home" / "projects.meta.json").unlink()

    runner = CliRunner()
    runner.invoke(app, ["project", "scan", str(work)])
    second = runner.invoke(app, ["project", "scan", str(work)])

    assert "already registered" in second.stdout
    assert len(reg.projects()) == 1


def test_scan_accepts_several_roots(tmp_path, monkeypatch):
    """Projects are rarely in one place — on Windows they span drives."""
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "home"))
    first, second = tmp_path / "d-drive", tmp_path / "e-drive"
    for base, name in ((first, "alpha"), (second, "beta")):
        (base / name).mkdir(parents=True)
        initialize(base / name)
    (tmp_path / "home" / "projects.json").unlink()
    (tmp_path / "home" / "projects.meta.json").unlink()

    result = CliRunner().invoke(
        app, ["project", "scan", str(first), str(second), "--depth", "2"]
    )

    assert result.exit_code == 0
    assert {r.path for r in reg.projects()} == {first / "alpha", second / "beta"}


def test_package_version_matches_pyproject():
    """These drift silently: `pjm doctor` printed 0.2.0 while shipping 0.3.0."""
    import re
    from pathlib import Path as _Path

    import projectmem

    pyproject = _Path(__file__).resolve().parents[1] / "pyproject.toml"
    declared = re.search(r'^version = "([^"]+)"', pyproject.read_text(), re.M).group(1)
    assert projectmem.__version__ == declared


def test_doctor_registers_what_it_finds(tmp_path, monkeypatch):
    """The upgrade path in one command."""
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    work = tmp_path / "work"
    (work / "one").mkdir(parents=True)
    initialize(work / "one")
    (tmp_path / "home" / "projects.json").unlink()
    (tmp_path / "home" / "projects.meta.json").unlink()

    runner = CliRunner()
    report = runner.invoke(app, ["doctor", "--path", str(work), "--depth", "2"])

    assert report.exit_code == 0
    assert "not registered" in report.stdout
    assert reg.projects() == ()          # reporting changes nothing

    fixed = runner.invoke(app, ["doctor", "--fix", "--path", str(work), "--depth", "2"])

    assert fixed.exit_code == 0
    assert [r.path for r in reg.projects()] == [work / "one"]


def test_doctor_prunes_entries_whose_memory_is_gone(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    gone = tmp_path / "deleted"
    gone.mkdir()
    initialize(gone)
    import shutil

    shutil.rmtree(gone / ".projectmem")

    result = CliRunner().invoke(
        app, ["doctor", "--fix", "--path", str(tmp_path / "empty-dir"), "--depth", "1"]
    )

    # a scan path that does not exist is fine; the stale check still runs
    assert reg.projects() == () or all(
        (r.path / ".projectmem").is_dir() for r in reg.projects()
    )


def test_upgrade_notice_fires_once(tmp_path, monkeypatch, capsys):
    """A wheel install runs no code of ours, so the CLI notices — but only once."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("PROJECTMEM_HOME", str(home))
    (home / "projects.json").write_text("[]", encoding="utf-8")
    (home / "projects.meta.json").write_text(
        json.dumps({"by_path": {}, "last_version": "0.2.0"}), encoding="utf-8"
    )

    from projectmem.cli import _upgrade_notice

    _upgrade_notice()
    first = capsys.readouterr().out
    _upgrade_notice()
    second = capsys.readouterr().out

    assert "upgraded to" in first and "pjm doctor" in first
    assert second == ""


def test_no_notice_on_a_first_ever_run(tmp_path, monkeypatch, capsys):
    """Nothing to upgrade from — `pjm init` already explains itself."""
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "home"))

    from projectmem.cli import _upgrade_notice

    _upgrade_notice()

    assert capsys.readouterr().out == ""


def test_default_depth_reaches_a_nested_repo_folder(tmp_path, monkeypatch):
    """D:\\repo\\Group\\Project is three levels down — a common Windows layout."""
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "home"))
    deep = tmp_path / "drive" / "repo" / "Group" / "Project"
    deep.mkdir(parents=True)
    initialize(deep)
    (tmp_path / "home" / "projects.json").unlink()
    (tmp_path / "home" / "projects.meta.json").unlink()

    result = CliRunner().invoke(
        app, ["doctor", "--fix", "--path", str(tmp_path / "drive")]
    )

    assert result.exit_code == 0
    assert [r.path for r in reg.projects()] == [deep]


def test_cloud_folders_are_included_by_default(tmp_path, monkeypatch):
    """Managed machines redirect Documents and Desktop into OneDrive."""
    home = tmp_path / "home"
    (home / "OneDrive - Some University").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    from projectmem.commands.doctor import default_roots

    assert home / "OneDrive - Some University" in default_roots()
