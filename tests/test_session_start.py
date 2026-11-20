from __future__ import annotations

from projectmem.storage import (
    ai_instructions_path,
    initialize,
    plan_path,
    summary_path,
)

PLAN = """# demo — plan

## Ideas
- maybe a cache

## Active plans
- [ ] ship the exporter

## Next
- review the exporter

## Someday / maybe
- rewrite in rust

## Shipped
- v1.0 released
"""


def _project(tmp_path):
    initialize(tmp_path)
    plan_path(tmp_path).write_text(PLAN, encoding="utf-8")
    summary_path(tmp_path).write_text("# summary\n\n- [OPEN] #0001 boom\n", encoding="utf-8")
    ai_instructions_path(tmp_path).write_text("# rules\n\nLog before you fix.\n", encoding="utf-8")
    return tmp_path


def test_start_session_contains_rules_summary_and_active_plan(tmp_path):
    from projectmem.session_start import build_session_start

    text = build_session_start(_project(tmp_path))

    assert "Log before you fix." in text
    assert "#0001 boom" in text
    assert "ship the exporter" in text
    assert "review the exporter" in text
    assert "get_project_map()" in text


def test_start_session_omits_ideas_shipped_and_someday(tmp_path):
    from projectmem.session_start import build_session_start

    text = build_session_start(_project(tmp_path))

    assert "maybe a cache" not in text
    assert "rewrite in rust" not in text
    assert "v1.0 released" not in text


def test_start_session_tolerates_missing_files(tmp_path):
    from projectmem.session_start import build_session_start

    initialize(tmp_path)
    plan_path(tmp_path).unlink(missing_ok=True)

    assert "get_project_map()" in build_session_start(tmp_path)


def test_server_instructions_name_start_session_and_allow_parallel_calls():
    from projectmem.mcp_server import mcp

    instructions = mcp.instructions or ""

    assert "start_session()" in instructions
    assert "Do NOT batch" not in instructions
    assert "in parallel" in instructions


def test_start_session_keeps_custom_plan_headings(tmp_path):
    from projectmem.session_start import build_session_start

    _project(tmp_path)
    plan_path(tmp_path).write_text(
        "# demo\n\n## In progress\n- [ ] migrate the queue\n\n## Shipped\n- v1\n",
        encoding="utf-8",
    )

    text = build_session_start(tmp_path)

    assert "migrate the queue" in text
    assert "- v1" not in text


def test_start_session_reads_a_plan_that_starts_with_a_heading(tmp_path):
    from projectmem.session_start import build_session_start

    _project(tmp_path)
    plan_path(tmp_path).write_text("## Active plans\n- [ ] first item\n", encoding="utf-8")

    assert "first item" in build_session_start(tmp_path)


def test_start_session_survives_an_empty_trailing_heading(tmp_path):
    from projectmem.session_start import build_session_start

    _project(tmp_path)
    plan_path(tmp_path).write_text("# demo\n## \n## Next\n- review\n", encoding="utf-8")

    assert "review" in build_session_start(tmp_path)


def test_start_session_says_the_session_start_reads_are_done(tmp_path):
    from projectmem.session_start import build_session_start

    text = build_session_start(_project(tmp_path))

    assert text.index("already") < text.index("Log before you fix.")
    assert "get_summary" in text.split("Log before you fix.")[0]


def test_start_session_includes_a_placeholder_map_for_setup_mode(tmp_path):
    from projectmem.session_start import build_session_start
    from projectmem.storage import project_map_path

    _project(tmp_path)
    project_map_path(tmp_path).write_text(
        "# Project map\n\nStatus: not created yet\n", encoding="utf-8"
    )

    assert "Status: not created yet" in build_session_start(tmp_path)


def test_start_session_leaves_out_a_real_map(tmp_path):
    from projectmem.session_start import build_session_start
    from projectmem.storage import project_map_path

    _project(tmp_path)
    project_map_path(tmp_path).write_text(
        "# Project map\n\n## Structure\n- src/app.py entry point\n", encoding="utf-8"
    )

    assert "src/app.py entry point" not in build_session_start(tmp_path)
