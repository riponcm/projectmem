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
