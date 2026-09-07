"""Turning an event location into a file path.

Two bugs lived in `location.split(":")[0]`, repeated at eleven call sites.

A bare `src/foo.py` has no colon, so score's file-coverage component counted
nothing — while `--at`'s own help text ("file:line, class.method") invites
exactly that form. It cost up to 20 points and weakened precheck's churn
detection, in silence.

And a Windows path carries a drive colon, so `C:\\src\\foo.py:42` split on the
first colon yields `"C"` — a phantom file, in staleness, precheck, brief,
context and export alike.
"""
from __future__ import annotations

import pytest

from projectmem.commands.score import calculate_score
from projectmem.models import location_to_file


@pytest.mark.parametrize(
    "location,expected",
    [
        ("src/foo.py", "src/foo.py"),              # the reported bug
        ("src/foo.py:42", "src/foo.py"),
        ("src/foo.py:42:7", "src/foo.py"),         # line:col
        ("foo.py", "foo.py"),                      # no directory
        ("deep/nested/path/mod.ts:1", "deep/nested/path/mod.ts"),
        (r"C:\src\foo.py", r"C:\src\foo.py"),      # drive colon is not a line
        (r"C:\src\foo.py:42", r"C:\src\foo.py"),
        ("D:/repo/app.tsx:9", "D:/repo/app.tsx"),
        (r"src\windows\rel.py:3", r"src\windows\rel.py"),
        ("AuthService.validate", None),            # a method, not a file
        ("SomeClass", None),
        ("", None),
        (None, None),
        ("   ", None),
    ],
)
def test_location_to_file(location, expected):
    assert location_to_file(location) == expected


def _score_with(location: str):
    return calculate_score([
        {"type": "issue", "summary": "broke", "location": location},
        {"type": "attempt", "outcome": "failed", "summary": "tried", "location": location},
    ])["components"]["files_with_gotchas"]


def test_a_bare_path_counts_the_same_as_one_with_a_line_number():
    """The regression. These two used to score 0 and 1."""
    assert _score_with("src/foo.py") == _score_with("src/foo.py:42") == 1


def test_a_windows_path_is_not_counted_as_a_file_called_c():
    assert _score_with(r"C:\src\payments\stripe.py:12") == 1


def test_a_method_location_is_not_a_file():
    assert _score_with("AuthService.validate") == 0


def test_the_same_file_by_two_spellings_counts_once():
    """`--at foo.py` on Monday and `--at foo.py:9` on Tuesday is one file."""
    result = calculate_score([
        {"type": "issue", "summary": "a", "location": "src/foo.py"},
        {"type": "attempt", "outcome": "failed", "summary": "b", "location": "src/foo.py:42"},
    ])
    assert result["components"]["files_with_gotchas"] == 1


# ── global memory honours $PROJECTMEM_HOME ──────────────────────────────────

def test_global_dir_follows_projectmem_home(tmp_path, monkeypatch):
    """It used to read a constant fixed at import from Path.home().

    append_event() auto-promotes library mentions into the global store, so a
    test running under an isolated $PROJECTMEM_HOME still wrote invented
    gotchas into the developer's real ~/.projectmem/global/.
    """
    from projectmem import global_memory

    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "iso"))
    assert global_memory.global_dir() == tmp_path / "iso" / "global"


def test_global_dir_falls_back_to_home_without_the_env_var(tmp_path, monkeypatch):
    from pathlib import Path

    from projectmem import global_memory

    monkeypatch.delenv("PROJECTMEM_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "h"))
    assert global_memory.global_dir() == tmp_path / "h" / ".projectmem" / "global"


def test_appending_an_event_stays_inside_an_isolated_home(tmp_path, monkeypatch):
    """The whole point: isolating the registry must isolate the gotchas too."""
    from pathlib import Path

    from projectmem.models import Event
    from projectmem.storage import append_event, initialize

    iso = tmp_path / "iso"
    monkeypatch.setenv("PROJECTMEM_HOME", str(iso))
    real_home = tmp_path / "real-home"
    (real_home / ".projectmem" / "global").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: real_home))

    project = tmp_path / "proj"
    project.mkdir()
    initialize(project)
    append_event(Event(type="note", summary="stripe needs an idempotency key"),
                 root=project)

    leaked = list((real_home / ".projectmem" / "global").iterdir())
    assert leaked == [], f"wrote into the real global store: {leaked}"
