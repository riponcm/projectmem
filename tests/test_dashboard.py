"""Tests for the cross-project registry + global dashboard (dashboard.py).

The PROJECTMEM_HOME registry is isolated per-test by conftest, so these never
touch the real ~/.projectmem/projects.json.
"""
import json
import re
import shutil

import pytest
import typer

from projectmem.storage import initialize, register_project, registered_projects
from projectmem.commands import dashboard as dashboard_command


def _resolved(projects):
    return {p.resolve() for p in projects}


def _make(root, name, events):
    """Create a registered project with a known set of events."""
    proj = root / name
    proj.mkdir()
    initialize(proj)  # scaffolds .projectmem AND registers the project
    (proj / ".projectmem" / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )
    return proj


def _data(index_path):
    html = index_path.read_text(encoding="utf-8")
    return json.loads(re.search(r"var DATA=(\{.*?\});", html, re.S).group(1))


ISSUE = {"id": "i1", "type": "issue", "summary": "bug",
         "timestamp": "2026-06-01T00:00:00Z", "issue_id": "0001"}
FAIL = {"id": "a1", "type": "attempt", "summary": "try",
        "timestamp": "2026-06-02T00:00:00Z", "outcome": "failed", "issue_id": "0001"}
FIX = {"id": "f1", "type": "fix", "summary": "fixed",
       "timestamp": "2026-06-03T00:00:00Z", "issue_id": "0001"}


# ── registry ────────────────────────────────────────────────────────────

def test_register_project_is_idempotent(tmp_path):
    p = tmp_path / "a"
    p.mkdir()
    initialize(p)
    register_project(p)
    register_project(p)  # again
    matches = [x for x in registered_projects() if x.resolve() == p.resolve()]
    assert len(matches) == 1


def test_init_registers_the_project(tmp_path):
    p = tmp_path / "proj"
    p.mkdir()
    initialize(p)
    assert p.resolve() in _resolved(registered_projects())


def test_registered_projects_skips_deleted_memory(tmp_path):
    p = tmp_path / "gone"
    p.mkdir()
    initialize(p)
    assert p.resolve() in _resolved(registered_projects())
    shutil.rmtree(p / ".projectmem")  # memory removed — path stays in registry
    assert p.resolve() not in _resolved(registered_projects())  # but reader filters it


# ── dashboard generation ────────────────────────────────────────────────

def test_dashboard_with_no_projects_exits_cleanly(tmp_path):
    with pytest.raises(typer.Exit):
        dashboard_command.run(output=tmp_path / "out", open_browser=False)


def test_dashboard_generates_index_and_per_project_dashboards(tmp_path):
    _make(tmp_path, "alpha", [ISSUE, FAIL, FIX])
    _make(tmp_path, "beta", [ISSUE, FIX])
    out = tmp_path / "gdash"
    dashboard_command.run(output=out, open_browser=False)

    assert (out / "index.html").exists()
    assert (out / "p0.html").exists() and (out / "p1.html").exists()
    data = _data(out / "index.html")
    assert data["agg"]["projects"] == 2
    assert len(data["projects"]) == 2
    assert all(p["href"] for p in data["projects"])  # every card is clickable


def test_dashboard_back_link_injected_into_project_dashboards(tmp_path):
    _make(tmp_path, "alpha", [ISSUE, FIX])
    out = tmp_path / "gdash"
    dashboard_command.run(output=out, open_browser=False)
    assert "All projects" in (out / "p0.html").read_text(encoding="utf-8")


def test_dashboard_open_and_fix_totals(tmp_path):
    _make(tmp_path, "alpha", [ISSUE, FIX])   # 1 issue closed by fix → 0 open, 1 fix
    _make(tmp_path, "beta", [ISSUE])         # 1 issue never closed  → 1 open, 0 fix
    out = tmp_path / "g"
    dashboard_command.run(output=out, open_browser=False)
    data = _data(out / "index.html")
    assert data["agg"]["open"] == 1
    assert data["agg"]["fixes"] == 1
    assert data["agg"]["events"] == 3  # 2 in alpha + 1 in beta


def test_dashboard_never_writes_to_the_projects(tmp_path):
    """The global view only READS each project — it must not mutate them."""
    proj = _make(tmp_path, "alpha", [ISSUE, FAIL, FIX])
    events_file = proj / ".projectmem" / "events.jsonl"
    before = events_file.read_text(encoding="utf-8")
    dashboard_command.run(output=tmp_path / "g", open_browser=False)
    assert events_file.read_text(encoding="utf-8") == before


# ── serve (live) vs static (snapshot) mode ──────────────────────────────

def _data_from_html(html):
    return json.loads(re.search(r"var DATA=(\{.*?\});", html, re.S).group(1))


def test_static_mode_marks_the_page_as_a_snapshot(tmp_path):
    """Default `pjm dashboard` is a serverless snapshot: live flag is False."""
    _make(tmp_path, "alpha", [ISSUE, FIX])
    html, count = dashboard_command._global_html()  # static default
    assert count == 1
    assert _data_from_html(html)["live"] is False


def test_serve_mode_marks_the_page_as_live(tmp_path):
    """`--serve` renders each load fresh, so the page advertises itself live."""
    _make(tmp_path, "alpha", [ISSUE, FIX])
    html, _ = dashboard_command._global_html(live=True)
    assert _data_from_html(html)["live"] is True


def test_back_linked_injects_before_body_and_is_safe_without_one():
    injected = dashboard_command._back_linked("<html><body>hi</body></html>")
    assert "All projects" in injected
    assert injected.index("All projects") < injected.index("</body>")
    # no </body> → returned untouched, never corrupted
    assert dashboard_command._back_linked("<div>x</div>") == "<div>x</div>"
