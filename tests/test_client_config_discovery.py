"""Finding the client configs — including the ones that are not where you look.

A Microsoft Store (MSIX) install of Claude Desktop runs in an AppContainer,
where the app's writes to %APPDATA% are redirected into the package's
LocalCache. Inside the container the config still appears at %APPDATA%\\Claude;
from any ordinary process it does not exist there at all. `pjm doctor` found
nothing and printed a clean bill of health while two pinned servers were
running — which is worse than missing the problem, because it ends the
investigation.

These build every path under tmp_path and patch the environment, so they run
anywhere. They are deliberately not gated on sys.platform: the bug is about
path construction, and that logic should be testable off Windows.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from conftest import set_fake_home
from projectmem.commands.init import (
    _client_configs,
    _pinned_client_configs,
    _projectmem_client_configs,
)

PINNED = ('{"mcpServers":{"projectmem":{"command":"py","args":'
          '["-m","projectmem.mcp_server","--root","D:/repo/Thing"]}}}')
CLEAN = '{"mcpServers":{"projectmem":{"command":"py","args":["-m","projectmem.mcp_server"]}}}'


@pytest.fixture
def win(tmp_path, monkeypatch):
    """A Windows-shaped environment with nothing pre-existing."""
    home = tmp_path / "home"
    home.mkdir()
    set_fake_home(monkeypatch, home)
    appdata = tmp_path / "Roaming"
    local = tmp_path / "Local"
    appdata.mkdir()
    local.mkdir()
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return home, appdata, local


def test_a_store_installed_claude_desktop_is_discovered(win):
    """The bug. Doctor reported green with a pinned server running."""
    _home, _appdata, local = win
    cfg = (local / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming"
           / "Claude" / "claude_desktop_config.json")
    cfg.parent.mkdir(parents=True)
    cfg.write_text(PINNED, encoding="utf-8")

    assert cfg in [p for _, p in _client_configs()]
    assert [p for _, p in _pinned_client_configs()] == [cfg]


def test_the_store_entry_is_labelled_distinctly(win):
    """The user is being sent to a path they have never seen."""
    _home, _appdata, local = win
    cfg = (local / "Packages" / "Claude_abc123" / "LocalCache" / "Roaming"
           / "Claude" / "claude_desktop_config.json")
    cfg.parent.mkdir(parents=True)
    cfg.write_text(PINNED, encoding="utf-8")

    assert [c for c, _ in _pinned_client_configs()] == ["Claude Desktop (Store)"]


def test_appdata_is_honoured_over_the_default_under_home(win):
    """The redirected-profile case: roaming profiles, OneDrive KFM, managed setups."""
    home, appdata, _local = win
    cfg = appdata / "Claude" / "claude_desktop_config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(PINNED, encoding="utf-8")

    # The old default location does not exist, and must not be needed.
    assert not (home / "AppData" / "Roaming" / "Claude").exists()
    assert [p for _, p in _pinned_client_configs()] == [cfg]


def test_several_store_packages_are_all_found_in_a_stable_order(win):
    _home, _appdata, local = win
    made = []
    for pkg in ("Claude_zzz999", "Claude_aaa111"):
        cfg = (local / "Packages" / pkg / "LocalCache" / "Roaming" / "Claude"
               / "claude_desktop_config.json")
        cfg.parent.mkdir(parents=True)
        cfg.write_text(PINNED, encoding="utf-8")
        made.append(cfg)

    found = [p for _, p in _pinned_client_configs()]
    assert sorted(made) == found, "globbed packages must come back sorted"


def test_no_packages_directory_is_not_an_error(win):
    """The common case everywhere except Windows."""
    _home, _appdata, local = win
    assert not (local / "Packages").exists()
    assert _pinned_client_configs() == []


def test_an_unreadable_config_is_reported_rather_than_skipped(win, monkeypatch):
    """"I could not look" must not read as "I looked and it is fine"."""
    _home, appdata, _local = win
    cfg = appdata / "Claude" / "claude_desktop_config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(PINNED, encoding="utf-8")

    real_read = Path.read_text

    def deny(self, *a, **kw):
        if self == cfg:
            raise PermissionError("access denied")
        return real_read(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", deny)

    states = {c: st for c, _, st in _projectmem_client_configs()}
    assert states.get("Claude Desktop") == "unreadable"
    # and it must not be silently counted as fine
    assert _pinned_client_configs() == []


def test_a_missing_config_says_nothing_at_all(win):
    """Not installed is not a problem to report."""
    assert _projectmem_client_configs() == []


def test_a_project_scoped_mcp_json_is_checked(tmp_path, monkeypatch):
    """A per-repo config pins just as effectively as a global one."""
    set_fake_home(monkeypatch, tmp_path / "home")
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("APPDATA", str(tmp_path / "none"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "none"))

    proj = tmp_path / "repo"
    proj.mkdir()
    (proj / ".mcp.json").write_text(PINNED, encoding="utf-8")

    assert _pinned_client_configs() == []            # not found without a root
    found = _pinned_client_configs(proj)
    assert [p for _, p in found] == [proj / ".mcp.json"]


def test_a_clean_project_config_is_not_flagged(tmp_path, monkeypatch):
    set_fake_home(monkeypatch, tmp_path / "home")
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("APPDATA", str(tmp_path / "none"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "none"))

    proj = tmp_path / "repo"
    proj.mkdir()
    (proj / ".mcp.json").write_text(CLEAN, encoding="utf-8")

    assert _pinned_client_configs(proj) == []
    assert [st for _, _, st in _projectmem_client_configs(proj)] == ["clean"]
