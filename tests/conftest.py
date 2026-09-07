"""Shared test fixtures.

Isolate the cross-project registry (and any $PROJECTMEM_HOME-scoped state) into a
per-test temp dir, so tests that run `pjm init` never write to the real user
registry at ~/.projectmem/projects.json.
"""
import pytest


@pytest.fixture(autouse=True)
def _isolate_projectmem_home(tmp_path_factory, monkeypatch):
    # Use a SEPARATE temp dir (not the test's own tmp_path), so a test that
    # chdir()s into its workdir doesn't see the registry home as a subfolder —
    # folder auto-detection would otherwise pick it up and change init output.
    home = tmp_path_factory.mktemp("pmhome")
    monkeypatch.setenv("PROJECTMEM_HOME", str(home))


def set_fake_home(monkeypatch, path) -> None:
    """Point Path.home() at `path` on every platform.

    Tests used to set $HOME alone. Windows ignores it — Path.home() reads
    %USERPROFILE% there — so the fixture home was silently unused and the real
    profile got scanned instead, which fails on a developer's machine and would
    scan their actual disk in CI. Setting both keeps one call site portable.
    """
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.setenv("USERPROFILE", str(path))
