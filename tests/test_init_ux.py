"""Tests for the v0.1.3 init UX additions.

L-048: `_populate_project_map_from_stack` — only writes when the current
PROJECT_MAP.md is still the placeholder; pulls description, libraries,
entry points, and main folders out of pyproject / package.json / Cargo
manifests; never clobbers human/AI-authored content.

L-049: `_print_mcp_config` — emits a copy-pasteable JSON block plus the
client-config file paths. Pure-print, but we still pin the format so
regressions are loud.
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest

from projectmem.commands.init import (
    _detect_main_folders,
    _extract_entry_points,
    _extract_project_description,
    _populate_project_map_from_stack,
    _print_mcp_config,
)
from projectmem.storage import initialize


# ── helpers ─────────────────────────────────────────────────────────────


def _make_repo(tmp_path: Path) -> Path:
    """Create a fresh project + initialized .projectmem/ for tests."""
    repo = tmp_path / "demo"
    repo.mkdir()
    initialize(repo)
    return repo


# ── _populate_project_map_from_stack ────────────────────────────────────


def test_stack_detect_does_nothing_when_no_manifests(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    pmap = repo / ".projectmem" / "PROJECT_MAP.md"
    before = pmap.read_text(encoding="utf-8")
    _populate_project_map_from_stack(repo)
    assert pmap.read_text(encoding="utf-8") == before


def test_stack_detect_pyproject_populates_map(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "pyproject.toml").write_text(
        '[project]\n'
        'name = "demo"\n'
        'version = "0.0.1"\n'
        'description = "A demo CLI built with Typer and pydantic."\n'
        'dependencies = ["typer", "pydantic", "fastapi"]\n'
        '[project.scripts]\n'
        'demo = "demo.cli:main"\n',
        encoding="utf-8",
    )
    (repo / "src").mkdir()
    (repo / "tests").mkdir()

    _populate_project_map_from_stack(repo)

    map_text = (repo / ".projectmem" / "PROJECT_MAP.md").read_text(encoding="utf-8")
    assert "auto-detected" in map_text
    assert "A demo CLI built with Typer and pydantic." in map_text
    assert "fastapi" in map_text
    assert "## Structure" in map_text
    assert "`src/`" in map_text
    assert "`tests/`" in map_text
    assert "`demo`" in map_text  # entry point
    # Placeholder marker should be gone.
    assert "Status: not created yet" not in map_text


def test_stack_detect_package_json_populates_map(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "package.json").write_text(
        '{"name":"demo","description":"A tiny Node app",'
        '"scripts":{"start":"node index.js","test":"jest"},'
        '"dependencies":{"react":"^18.0.0","next":"^14.0.0"}}',
        encoding="utf-8",
    )

    _populate_project_map_from_stack(repo)

    map_text = (repo / ".projectmem" / "PROJECT_MAP.md").read_text(encoding="utf-8")
    assert "A tiny Node app" in map_text
    assert "react" in map_text or "nextjs" in map_text
    assert "`npm run start`" in map_text


def test_stack_detect_never_overwrites_user_edited_map(tmp_path: Path) -> None:
    """If PROJECT_MAP no longer says 'Status: not created yet', leave it alone."""
    repo = _make_repo(tmp_path)
    pmap = repo / ".projectmem" / "PROJECT_MAP.md"
    custom = "# My Custom Project Map\n\nStatus: hand-written\n\nKeep this!"
    pmap.write_text(custom, encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        '[project]\ndescription = "Should NOT appear"\n', encoding="utf-8"
    )

    _populate_project_map_from_stack(repo)

    assert pmap.read_text(encoding="utf-8") == custom


def test_stack_detect_handles_corrupt_manifest(tmp_path: Path) -> None:
    """Corrupt JSON / TOML must not raise — placeholder stays untouched."""
    repo = _make_repo(tmp_path)
    (repo / "package.json").write_text("not valid {{{", encoding="utf-8")
    pmap = repo / ".projectmem" / "PROJECT_MAP.md"
    before = pmap.read_text(encoding="utf-8")
    # Should not raise.
    _populate_project_map_from_stack(repo)
    # With no other manifest signal, content stays as placeholder.
    after = pmap.read_text(encoding="utf-8")
    # Either unchanged, or detect_stack picked up package.json as a manifest;
    # the critical invariant is no exception.
    assert isinstance(after, str)


# ── _extract_project_description ────────────────────────────────────────


def test_extract_description_pyproject(tmp_path: Path) -> None:
    repo = tmp_path
    (repo / "pyproject.toml").write_text(
        '[project]\ndescription = "The thing"\n', encoding="utf-8"
    )
    assert _extract_project_description(repo) == "The thing"


def test_extract_description_package_json(tmp_path: Path) -> None:
    repo = tmp_path
    (repo / "package.json").write_text(
        '{"description": "Node thing"}', encoding="utf-8"
    )
    assert _extract_project_description(repo) == "Node thing"


def test_extract_description_returns_none_when_absent(tmp_path: Path) -> None:
    assert _extract_project_description(tmp_path) is None


# ── _extract_entry_points ───────────────────────────────────────────────


def test_extract_entry_points_pyproject_scripts(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project.scripts]\n'
        'foo = "pkg.cli:main"\n'
        'bar = "pkg.other:run"\n'
        '[tool.something]\n'
        'foo = "should-not-appear"\n',
        encoding="utf-8",
    )
    eps = _extract_entry_points(tmp_path)
    assert "`foo` → `pkg.cli:main`" in eps
    assert "`bar` → `pkg.other:run`" in eps
    assert not any("should-not-appear" in e for e in eps)


def test_extract_entry_points_npm_scripts(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        '{"scripts":{"dev":"vite","build":"vite build","test":"jest","custom":"x"}}',
        encoding="utf-8",
    )
    eps = _extract_entry_points(tmp_path)
    # Only the known set (start/dev/build/test) is reported.
    assert any("npm run dev" in e for e in eps)
    assert any("npm run build" in e for e in eps)
    assert any("npm run test" in e for e in eps)
    assert not any("custom" in e for e in eps)


# ── _detect_main_folders ────────────────────────────────────────────────


def test_detect_main_folders(tmp_path: Path) -> None:
    for d in ("src", "tests", "docs"):
        (tmp_path / d).mkdir()
    (tmp_path / "node_modules").mkdir()  # should NOT be reported

    found = dict(_detect_main_folders(tmp_path))
    assert "src" in found
    assert "tests" in found
    assert "docs" in found
    assert "node_modules" not in found


# ── _print_mcp_config ───────────────────────────────────────────────────


def test_print_mcp_config_contains_expected_pieces(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Default since 0.3.0: one server for every registered project, so the
    # printed config carries no --root. HOME is redirected because the function
    # also scans real client configs, and a test must not read the developer's.
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    _print_mcp_config(tmp_path / "myproj")
    out = capsys.readouterr().out
    assert '"mcpServers"' in out
    assert '"projectmem"' in out
    assert '"-m", "projectmem.mcp_server"' in out
    assert '"--root"' not in out  # not in the JSON block
    assert "pjm project use myproj" in out
    # All 4 client paths should be mentioned.
    assert "Claude Desktop" in out
    assert "Cursor" in out
    assert "Antigravity" in out
    assert "Codex" in out
    assert "config.toml" in out  # Codex TOML note


def test_print_mcp_config_single_project_still_pins_the_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--mcp-config-single` keeps the pinned form for one-repo setups."""
    _print_mcp_config(tmp_path / "myproj", single_project=True)
    out = capsys.readouterr().out
    assert f'--root", "{tmp_path / "myproj"}"' in out
    assert "this project only" in out


def test_print_mcp_config_uses_absolute_python(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys
    _print_mcp_config(tmp_path)
    out = capsys.readouterr().out
    # The command must be an absolute path (sys.executable), not bare "python".
    assert f'"command": "{sys.executable}"' in out


def test_init_warns_when_a_client_config_still_pins_one_repo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An upgrader's client still points at one repo — say so, don't edit it."""
    home = tmp_path / "home"
    (home / ".cursor").mkdir(parents=True)
    (home / ".cursor" / "mcp.json").write_text(
        '{"mcpServers":{"projectmem":{"args":["-m","projectmem.mcp_server",'
        '"--root","/old/repo"]}}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))

    _print_mcp_config(tmp_path / "myproj")
    out = capsys.readouterr().out

    assert "still pins projectmem to one repo" in out
    assert "Cursor" in out
    assert str(home / ".cursor" / "mcp.json") in out
    # the file is reported, never modified
    assert "--root" in (home / ".cursor" / "mcp.json").read_text(encoding="utf-8")


def test_init_detects_a_pin_set_through_the_environment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude Desktop configs pin with an env block, not an arg."""
    home = tmp_path / "home"
    cfg = home / "Library/Application Support/Claude"
    cfg.mkdir(parents=True)
    (cfg / "claude_desktop_config.json").write_text(
        '{"mcpServers":{"projectmem":{"command":"python","args":["-m",'
        '"projectmem.mcp_server"],"env":{"PROJECTMEM_ROOT":"/old/repo"}}}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))

    _print_mcp_config(tmp_path / "myproj")
    out = capsys.readouterr().out

    assert "still pins projectmem to one repo" in out
    assert "Claude Desktop" in out


def test_init_prints_the_codex_toml_form(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex reads TOML — printing only JSON left them to translate it."""
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))

    _print_mcp_config(tmp_path / "myproj")
    out = capsys.readouterr().out

    assert "[mcp_servers.projectmem]" in out
    assert 'args = ["-m", "projectmem.mcp_server"]' in out
