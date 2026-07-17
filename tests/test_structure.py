"""Tests for the code-structure extractor (structure.py)."""
from projectmem.structure import build_structure, write_structure
from projectmem.storage import initialize


def _make_project(root):
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("from pkg import b\n", encoding="utf-8")
    (pkg / "a.py").write_text(
        "import os\nfrom pkg import b\nfrom .c import thing\n", encoding="utf-8"
    )
    (pkg / "b.py").write_text("x = 1\n", encoding="utf-8")
    (pkg / "c.py").write_text("thing = 1\n", encoding="utf-8")
    # noise that must be excluded
    (root / "venv").mkdir()
    (root / "venv" / "junk.py").write_text("import sys\n", encoding="utf-8")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "x.py").write_text("", encoding="utf-8")


def test_walk_finds_source_and_excludes_noise(tmp_path):
    _make_project(tmp_path)
    data = build_structure(tmp_path)
    files = set(data["files"])
    assert {"pkg/a.py", "pkg/b.py", "pkg/c.py"} <= files
    assert not any("venv" in f for f in files)
    assert not any("__pycache__" in f for f in files)
    assert data["stats"]["files"] == len(files)


def test_python_imports_resolve_to_internal_files_only(tmp_path):
    _make_project(tmp_path)
    rels = {(r["source"], r["target"]) for r in build_structure(tmp_path)["relationships"]}
    # `from pkg import b`  and  relative `from .c import thing`
    assert ("pkg/a.py", "pkg/b.py") in rels
    assert ("pkg/a.py", "pkg/c.py") in rels
    # stdlib / third-party imports (os, sys) are NOT project structure
    targets = {t for _, t in rels}
    assert "os" not in targets and "sys" not in targets


def test_write_structure_writes_gitignored_cache(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_project(tmp_path)
    initialize(tmp_path)
    out, data = write_structure(tmp_path)
    assert out.exists() and out.name == "structure.json"
    assert data["stats"]["relationships"] >= 2
    # structure.json is a derived cache — must be gitignored
    assert ".projectmem/structure.json" in (tmp_path / ".gitignore").read_text(encoding="utf-8")


def test_extractor_never_touches_events(tmp_path, monkeypatch):
    """INVARIANT: the parser writes only structure.json, never memory."""
    monkeypatch.chdir(tmp_path)
    _make_project(tmp_path)
    initialize(tmp_path)
    events_before = (tmp_path / ".projectmem" / "events.jsonl").read_text(encoding="utf-8")
    write_structure(tmp_path)
    events_after = (tmp_path / ".projectmem" / "events.jsonl").read_text(encoding="utf-8")
    assert events_before == events_after
