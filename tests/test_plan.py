"""Tests for the plan.md intent-file feature."""
from projectmem.storage import initialize, plan_path
from projectmem.commands import plan as plan_command


def test_init_creates_plan_md(tmp_path):
    initialize(tmp_path)
    p = plan_path(tmp_path)
    assert p.exists()
    text = p.read_text(encoding="utf-8")
    assert "— plan" in text
    # the core sections
    for section in ("## Ideas", "## Active plans", "## Next", "## Shipped"):
        assert section in text
    # it must state it is NOT the event log
    assert "NOT the event log" in text


def test_plan_md_is_committed_not_gitignored(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    initialize(tmp_path)
    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    # plan.md is shared intent — committed like PROJECT_MAP.md, NOT ignored.
    assert "plan.md" not in gitignore


def test_pjm_plan_add_appends_to_ideas(tmp_path):
    initialize(tmp_path)
    plan_command.run(add="add an export/migration adapter", root=tmp_path)
    text = plan_path(tmp_path).read_text(encoding="utf-8")
    assert "- add an export/migration adapter" in text
    # appended under Ideas, not anywhere else
    ideas_block = text.split("## Ideas", 1)[1].split("##", 1)[0]
    assert "add an export/migration adapter" in ideas_block


def test_plan_add_does_not_touch_events(tmp_path):
    """A plan is intent, not memory — it must never become an event."""
    initialize(tmp_path)
    events = tmp_path / ".projectmem" / "events.jsonl"
    before = events.read_text(encoding="utf-8")
    plan_command.run(add="ship the plan.md feature", root=tmp_path)
    assert events.read_text(encoding="utf-8") == before


def test_get_plan_mcp_reads_the_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    initialize(tmp_path)
    from projectmem import mcp_server
    out = mcp_server.get_plan()
    assert "— plan" in out and "## Ideas" in out
