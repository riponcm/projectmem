from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

import projectmem.commands.project as project_commands
import projectmem.project_registry as project_registry
from projectmem.cli import app


runner = CliRunner()


def _initialized_repo(tmp_path: Path, name: str = "demo") -> Path:
    repo = tmp_path / name
    mem_dir = repo / ".projectmem"
    mem_dir.mkdir(parents=True)
    (mem_dir / "config.toml").write_text("", encoding="utf-8")
    return repo


def test_project_register_list_and_use(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "home"))
    repo = _initialized_repo(tmp_path)

    registered = runner.invoke(
        app,
        [
            "project",
            "register",
            str(repo),
            "--brain",
            "personal",
            "--tag",
            "Python",
            "--tag",
            "Local First",
        ],
    )
    assert registered.exit_code == 0

    listed = runner.invoke(
        app,
        ["project", "list", "--brain", "personal", "--tag", "python"],
    )
    assert listed.exit_code == 0
    assert all(
        column in listed.stdout
        for column in ("ACTIVE", "ALIAS", "BRAIN", "TAGS", "PATH")
    )
    assert "local-first,python" in listed.stdout

    selected = runner.invoke(app, ["project", "use", repo.name.upper()])
    assert selected.exit_code == 0
    assert repo.name in selected.stdout
    assert "*" in runner.invoke(app, ["project", "list"]).stdout


def test_project_list_uses_one_registry_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "home"))
    repo = _initialized_repo(tmp_path)
    assert runner.invoke(app, ["project", "register", str(repo)]).exit_code == 0

    real_load = project_registry.load_registry
    load_count = 0

    def counted_load(*args, **kwargs):
        nonlocal load_count
        load_count += 1
        return real_load(*args, **kwargs)

    monkeypatch.setattr(project_registry, "load_registry", counted_load)
    monkeypatch.setattr(project_commands, "load_registry", counted_load)

    result = runner.invoke(app, ["project", "list"])

    assert result.exit_code == 0
    assert load_count == 1


def test_project_detect_unregistered_prints_exact_command(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "home"))
    repo = _initialized_repo(tmp_path)

    result = runner.invoke(app, ["project", "detect", "--path", str(repo)])

    assert result.exit_code == 1
    assert f'pjm project register "{repo.resolve()}"' in result.output


def test_project_detect_unregistered_nested_file_prints_root_command(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "home"))
    repo = _initialized_repo(tmp_path)
    target = repo / "src" / "module.py"
    target.parent.mkdir()
    target.write_text("", encoding="utf-8")

    result = runner.invoke(
        app,
        ["project", "detect", "--path", str(target)],
    )

    assert result.exit_code == 1
    assert f'pjm project register "{repo.resolve()}"' in result.output
    assert str(target.resolve()) not in result.output


def test_project_detect_invalid_path_exits_one(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "home"))

    result = runner.invoke(
        app,
        ["project", "detect", "--path", str(tmp_path / "missing")],
    )

    assert result.exit_code == 1
    assert "does not exist" in result.output


def test_project_detect_uninitialized_path_does_not_suggest_registration(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "home"))
    uninitialized = tmp_path / "plain"
    uninitialized.mkdir()

    result = runner.invoke(
        app,
        ["project", "detect", "--path", str(uninitialized)],
    )

    assert result.exit_code == 1
    assert "not initialized" in result.output
    assert "pjm project register" not in result.output


def test_project_detect_does_not_treat_global_store_as_project(
    tmp_path, monkeypatch
):
    simulated_home = tmp_path / "home"
    (simulated_home / ".projectmem").mkdir(parents=True)
    target = simulated_home / "work" / "project"
    target.mkdir(parents=True)
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "registry-home"))

    result = runner.invoke(
        app,
        ["project", "detect", "--path", str(target)],
    )

    assert result.exit_code == 1
    assert "not initialized" in result.output
    assert (
        f'pjm project register "{simulated_home.resolve()}"'
        not in result.output
    )


def test_project_detect_registered_nested_file_succeeds(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "home"))
    repo = _initialized_repo(tmp_path)
    target = repo / "src" / "module.py"
    target.parent.mkdir()
    target.write_text("", encoding="utf-8")
    assert runner.invoke(app, ["project", "register", str(repo)]).exit_code == 0

    result = runner.invoke(
        app,
        ["project", "detect", "--path", str(target)],
    )

    assert result.exit_code == 0
    assert repo.name in result.output
    assert str(repo.resolve()) in result.output


def test_project_brain_and_tag_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "home"))
    repo = _initialized_repo(tmp_path)
    assert runner.invoke(app, ["project", "register", str(repo)]).exit_code == 0

    assert (
        runner.invoke(
            app, ["project", "set-brain", repo.name, "personal"]
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            app, ["project", "tag", "add", repo.name, "Local First"]
        ).exit_code
        == 0
    )
    tags = runner.invoke(app, ["project", "tag", "list", repo.name])
    assert tags.exit_code == 0
    assert tags.stdout.strip() == "local-first"
    assert (
        runner.invoke(
            app, ["project", "tag", "remove", repo.name, "local-first"]
        ).exit_code
        == 0
    )


def test_project_tag_list_missing_project_is_actionable(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "home"))

    result = runner.invoke(app, ["project", "tag", "list", "missing"])

    assert result.exit_code == 1
    assert result.output.splitlines() == [
        "Project is not registered: missing. Run pjm project list."
    ]


def test_project_remove_and_domain_error_exit_one(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "home"))
    repo = _initialized_repo(tmp_path)
    runner.invoke(app, ["project", "register", str(repo)])

    removed = runner.invoke(app, ["project", "remove", repo.name])
    assert removed.exit_code == 0

    missing = runner.invoke(app, ["project", "use", repo.name])
    assert missing.exit_code == 1
    assert "not registered" in missing.output


def test_project_list_invalid_utf8_registry_exits_one(tmp_path, monkeypatch):
    projectmem_home = tmp_path / "home"
    projectmem_home.mkdir()
    (projectmem_home / "projects.json").write_bytes(b"\xff")
    monkeypatch.setenv("PROJECTMEM_HOME", str(projectmem_home))

    result = runner.invoke(app, ["project", "list"])

    assert result.exit_code == 1
    assert "valid UTF-8" in result.output
    assert "Traceback" not in result.output


def test_project_list_validates_filters_before_loading_registry(
    tmp_path, monkeypatch
):
    projectmem_home = tmp_path / "home"
    projectmem_home.mkdir()
    (projectmem_home / "projects.json").write_text(
        "{not json",
        encoding="utf-8",
    )
    monkeypatch.setenv("PROJECTMEM_HOME", str(projectmem_home))

    result = runner.invoke(
        app,
        ["project", "list", "--brain", "invalid"],
    )

    assert result.exit_code == 1
    assert "brain" in result.output
    assert "malformed JSON" not in result.output


def test_project_syntax_errors_remain_exit_two():
    result = runner.invoke(app, ["project", "set-brain"])
    assert result.exit_code == 2
