from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from projectmem.cli import app


runner = CliRunner()


def _initialized_repo(tmp_path: Path, name: str = "demo") -> Path:
    repo = tmp_path / name
    (repo / ".projectmem").mkdir(parents=True)
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


def test_project_detect_unregistered_prints_exact_command(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "home"))
    repo = _initialized_repo(tmp_path)

    result = runner.invoke(app, ["project", "detect", "--path", str(repo)])

    assert result.exit_code == 1
    assert f'pjm project register "{repo.resolve()}"' in result.output


def test_project_detect_invalid_path_exits_one(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "home"))

    result = runner.invoke(
        app,
        ["project", "detect", "--path", str(tmp_path / "missing")],
    )

    assert result.exit_code == 1
    assert "does not exist" in result.output


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


def test_project_remove_and_domain_error_exit_one(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "home"))
    repo = _initialized_repo(tmp_path)
    runner.invoke(app, ["project", "register", str(repo)])

    removed = runner.invoke(app, ["project", "remove", repo.name])
    assert removed.exit_code == 0

    missing = runner.invoke(app, ["project", "use", repo.name])
    assert missing.exit_code == 1
    assert "not registered" in missing.output


def test_project_syntax_errors_remain_exit_two():
    result = runner.invoke(app, ["project", "set-brain"])
    assert result.exit_code == 2
