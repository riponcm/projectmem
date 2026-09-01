from __future__ import annotations

from pathlib import Path

from projectmem.commands.visualize import _location_path_for_graph, build_graph_data
from projectmem.models import Event


def test_location_path_for_graph_accepts_file_path_without_line(tmp_path: Path) -> None:
    source = tmp_path / "src" / "agent_portability_kit" / "importers" / "neutral.py"
    source.parent.mkdir(parents=True)
    source.write_text("# fixture\n", encoding="utf-8")

    result = _location_path_for_graph(
        "src/agent_portability_kit/importers/neutral.py",
        root=tmp_path,
    )

    assert result == "src/agent_portability_kit/importers/neutral.py"


def test_location_path_for_graph_accepts_file_path_with_line(tmp_path: Path) -> None:
    source = tmp_path / "src" / "projectmem" / "cli.py"
    source.parent.mkdir(parents=True)
    source.write_text("# fixture\n", encoding="utf-8")

    result = _location_path_for_graph("src/projectmem/cli.py:42", root=tmp_path)

    assert result == "src/projectmem/cli.py"


def test_location_path_for_graph_normalizes_windows_separators(tmp_path: Path) -> None:
    source = tmp_path / "src" / "projectmem" / "cli.py"
    source.parent.mkdir(parents=True)
    source.write_text("# fixture\n", encoding="utf-8")

    result = _location_path_for_graph(r".\src\projectmem\cli.py", root=tmp_path)

    assert result == "src/projectmem/cli.py"


def test_location_path_for_graph_accepts_existing_directory(tmp_path: Path) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()

    result = _location_path_for_graph("tests", root=tmp_path)

    assert result == "tests/"


def test_location_path_for_graph_rejects_descriptive_locations(tmp_path: Path) -> None:
    assert _location_path_for_graph("projectmem pre-commit hook", root=tmp_path) is None
    assert _location_path_for_graph("docs current-state", root=tmp_path) is None
    assert _location_path_for_graph("deploy pipeline", root=tmp_path) is None


def _node_ids(graph: dict) -> set[str]:
    return {node["id"] for node in graph["nodes"]}


def _link_tuples(graph: dict) -> set[tuple[str, str, str]]:
    return {
        (link["source"], link["target"], link["type"])
        for link in graph["links"]
    }


def _node_by_id(graph: dict, node_id: str) -> dict:
    return next(node for node in graph["nodes"] if node["id"] == node_id)


def _events_for_file(path: str, count: int, *, failed: int = 0) -> list[Event]:
    events = []
    for index in range(count):
        outcome = "failed" if index < failed else "worked"
        events.append(
            Event(
                id=f"evt_{path.replace('/', '_').replace('.', '_')}_{index}",
                type="attempt",
                outcome=outcome,
                files=[path],
                summary=f"Attempt {index} for {path}",
            )
        )
    return events


def test_build_graph_data_adds_file_directory_metadata(tmp_path: Path) -> None:
    event = Event(
        id="evt_metadata",
        type="note",
        files=["src/projectmem/commands/visualize.py"],
        summary="Story Map needs directory metadata",
    )

    graph = build_graph_data([event], root=tmp_path)

    file_node = _node_by_id(graph, "src/projectmem/commands/visualize.py")
    assert file_node["path"] == "src/projectmem/commands/visualize.py"
    assert file_node["directory_parts"] == ["src", "projectmem", "commands"]
    assert file_node["top_directory"] == "src/"
    assert file_node["event_count"] == 1
    assert file_node["failure_count"] == 0
    assert file_node["importance"] == 1


def test_build_graph_data_groups_root_level_files_under_root_bucket(
    tmp_path: Path,
) -> None:
    event = Event(
        id="evt_root",
        type="note",
        files=["README.md"],
        summary="Root files should have a stable directory bucket",
    )

    graph = build_graph_data([event], root=tmp_path)

    file_node = _node_by_id(graph, "README.md")
    assert file_node["directory_parts"] == []
    assert file_node["top_directory"] == "./"


def test_build_graph_data_counts_all_attached_events_for_dense_files(
    tmp_path: Path,
) -> None:
    events = _events_for_file("src/projectmem/commands/visualize.py", 10)

    graph = build_graph_data(events, root=tmp_path)

    file_node = _node_by_id(graph, "src/projectmem/commands/visualize.py")
    assert file_node["event_count"] == 10
    assert file_node["dense_event_threshold"] == 10
    assert file_node["is_dense"] is True


def test_build_graph_data_marks_nine_events_as_not_dense(tmp_path: Path) -> None:
    events = _events_for_file("src/projectmem/commands/visualize.py", 9)

    graph = build_graph_data(events, root=tmp_path)

    file_node = _node_by_id(graph, "src/projectmem/commands/visualize.py")
    assert file_node["event_count"] == 9
    assert file_node["is_dense"] is False


def test_build_graph_data_importance_includes_failure_weight(
    tmp_path: Path,
) -> None:
    events = _events_for_file("src/projectmem/mcp_server.py", 6, failed=4)

    graph = build_graph_data(events, root=tmp_path)

    file_node = _node_by_id(graph, "src/projectmem/mcp_server.py")
    assert file_node["event_count"] == 6
    assert file_node["failure_count"] == 4
    assert file_node["importance"] == 18


def test_build_graph_data_counts_same_file_and_location_once_per_event(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src" / "projectmem" / "commands" / "visualize.py"
    source.parent.mkdir(parents=True)
    source.write_text("# fixture\n", encoding="utf-8")
    event = Event(
        id="evt_duplicate_file_location",
        type="attempt",
        files=[r"src\projectmem\commands\visualize.py"],
        location="src/projectmem/commands/visualize.py:10",
        outcome="failed",
        summary="Auto-capture referenced the same file twice",
    )

    graph = build_graph_data([event], root=tmp_path)

    file_node_ids = [node["id"] for node in graph["nodes"] if node["type"] == "file"]
    assert file_node_ids == ["src/projectmem/commands/visualize.py"]
    file_node = _node_by_id(graph, "src/projectmem/commands/visualize.py")
    assert file_node["event_count"] == 1
    assert file_node["failure_count"] == 1
    assert file_node["failures"] == 1
    assert file_node["importance"] == 4


def test_build_graph_data_deduplicates_duplicate_file_links(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src" / "projectmem" / "commands" / "visualize.py"
    source.parent.mkdir(parents=True)
    source.write_text("# fixture\n", encoding="utf-8")
    event = Event(
        id="evt_duplicate_links",
        type="attempt",
        files=[
            "src/projectmem/commands/visualize.py",
            r".\src\projectmem\commands\visualize.py",
        ],
        location="src/projectmem/commands/visualize.py:10",
        outcome="failed",
        summary="Auto-capture produced duplicate file links",
    )

    graph = build_graph_data([event], root=tmp_path)

    matching_links = [
        link
        for link in graph["links"]
        if link["source"] == "evt_duplicate_links"
        and link["target"] == "src/projectmem/commands/visualize.py"
    ]
    assert matching_links == [
        {
            "source": "evt_duplicate_links",
            "target": "src/projectmem/commands/visualize.py",
            "type": "mention",
        }
    ]


def test_build_graph_data_links_path_like_location_without_line(tmp_path: Path) -> None:
    source = tmp_path / "src" / "agent_portability_kit" / "importers" / "neutral.py"
    source.parent.mkdir(parents=True)
    source.write_text("# fixture\n", encoding="utf-8")
    event = Event(
        id="evt_24214d5d29b2483e8da2",
        type="attempt",
        issue_id="0008",
        location="src/agent_portability_kit/importers/neutral.py",
        outcome="worked",
        summary=(
            "Tightened neutral import to reject missing required fields, extra "
            "closed-shape keys, wrong MCP string types, and forbidden "
            "transport-branch keys"
        ),
    )

    graph = build_graph_data([event], root=tmp_path)

    assert "evt_24214d5d29b2483e8da2" in _node_ids(graph)
    assert "src/agent_portability_kit/importers/neutral.py" in _node_ids(graph)
    assert (
        "evt_24214d5d29b2483e8da2",
        "src/agent_portability_kit/importers/neutral.py",
        "at",
    ) in _link_tuples(graph)


def test_build_graph_data_keeps_descriptive_location_unlinked(tmp_path: Path) -> None:
    event = Event(
        id="evt_descriptive",
        type="note",
        location="projectmem pre-commit hook",
        summary="Windows CP1252 terminal output can fail on box drawing characters",
    )

    graph = build_graph_data([event], root=tmp_path)

    assert "evt_descriptive" in _node_ids(graph)
    assert "projectmem pre-commit hook" not in _node_ids(graph)
    assert graph["links"] == []


def test_build_graph_data_counts_failed_attempts_for_path_like_location(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src" / "projectmem" / "commands" / "visualize.py"
    source.parent.mkdir(parents=True)
    source.write_text("# fixture\n", encoding="utf-8")
    event = Event(
        id="evt_failed",
        type="attempt",
        location="src/projectmem/commands/visualize.py",
        outcome="failed",
        summary="Tried linking only file:line locations and left file-only events floating",
    )

    graph = build_graph_data([event], root=tmp_path)

    file_node = next(
        node
        for node in graph["nodes"]
        if node["id"] == "src/projectmem/commands/visualize.py"
    )
    assert file_node["failures"] == 1


def test_build_graph_data_still_links_explicit_files(tmp_path: Path) -> None:
    event = Event(
        id="evt_files",
        type="fix",
        files=["README.md", "src/projectmem/cli.py"],
        summary="Backfilled commit touched README and CLI",
    )

    graph = build_graph_data([event], root=tmp_path)

    assert ("evt_files", "README.md", "mention") in _link_tuples(graph)
    assert ("evt_files", "src/projectmem/cli.py", "mention") in _link_tuples(graph)


def test_build_graph_data_still_links_location_with_line(tmp_path: Path) -> None:
    source = tmp_path / "src" / "projectmem" / "cli.py"
    source.parent.mkdir(parents=True)
    source.write_text("# fixture\n", encoding="utf-8")
    event = Event(
        id="evt_line",
        type="issue",
        location="src/projectmem/cli.py:210",
        summary="Visualize command needs graph payload fix",
    )

    graph = build_graph_data([event], root=tmp_path)

    assert ("evt_line", "src/projectmem/cli.py", "at") in _link_tuples(graph)


def test_template_has_project_map_flow_view() -> None:
    """The Project Map ships three layouts: Tree, Graph, and Flow (0.1.6)."""
    from projectmem.commands.visualize import VIZ_TEMPLATE

    assert 'data-view="tree"' in VIZ_TEMPLATE
    assert 'data-view="graph"' in VIZ_TEMPLATE
    assert 'data-view="flow"' in VIZ_TEMPLATE
    assert 'id="map-flow"' in VIZ_TEMPLATE
    assert "renderMapFlow" in VIZ_TEMPLATE
    # the flow ends in the append-only memory cylinder
    assert "events.jsonl" in VIZ_TEMPLATE


def test_template_has_timeline_spine_view() -> None:
    """Timeline ships two views: Spine (default) and the Details list (0.1.6)."""
    from projectmem.commands.visualize import VIZ_TEMPLATE

    assert 'data-tlview="spine"' in VIZ_TEMPLATE
    assert 'data-tlview="list"' in VIZ_TEMPLATE
    assert 'id="tl-spine"' in VIZ_TEMPLATE
    assert "renderTimelineSpine" in VIZ_TEMPLATE
    # spine defaults to active; Flow is the Project Map default
    assert VIZ_TEMPLATE.index('data-view="flow"') < VIZ_TEMPLATE.index('data-view="tree"')


# ── Untrusted event text must never reach the DOM as markup ──
# Event summaries come from git commit messages, CI output and AI agents, and
# the dashboard drops them straight into innerHTML. Two failure modes, both
# reproduced against 0.2.0: a `</script>` in a summary closed the data <script>
# early (whole dashboard died with a SyntaxError), and an `<img onerror>` in a
# summary executed when the Timeline rendered.

HOSTILE = '<img src=x onerror="window.__pwned=1"></script><script>window.__pwned=1</script>'


def _viz_html(tmp_path: Path) -> str:
    from projectmem.commands import visualize as visualize_command
    from projectmem.models import Event
    from projectmem.storage import append_event, initialize

    initialize(tmp_path)
    append_event(
        Event(type="issue", summary=HOSTILE, location=HOSTILE, issue_id="0001"),
        root=tmp_path,
    )
    (tmp_path / ".projectmem" / "PROJECT_MAP.md").write_text(
        f"# Map\n\n- {HOSTILE}\n", encoding="utf-8"
    )
    visualize_command.run(root=tmp_path, open_browser=False)
    return (tmp_path / ".projectmem" / "viz.html").read_text(encoding="utf-8")


def test_viz_data_cannot_close_the_script_tag(tmp_path: Path) -> None:
    html = _viz_html(tmp_path)

    # Only the two real <script> tags in the template may close one.
    assert html.count("</script>") == 2


def test_viz_embeds_hostile_text_escaped(tmp_path: Path) -> None:
    html = _viz_html(tmp_path)

    # The payload survives as data (escaped), never as literal markup.
    assert "<img src=x" not in html
    assert "\\u003cimg src=x" in html


def test_viz_renders_event_text_through_the_escaper(tmp_path: Path) -> None:
    html = _viz_html(tmp_path)

    # Timeline rows and Story Map tooltips are the innerHTML sinks that a
    # hostile summary reached in 0.2.0.
    assert "pmEsc(e.summary)" in html
    assert "+e.summary+" not in html
    assert "pmEsc(d.summary" in html


def test_json_for_script_escapes_tag_characters() -> None:
    from projectmem.commands.visualize import json_for_script

    import json as _json

    payload = json_for_script({"s": "</script><script>alert(1)</script>"})

    assert "<" not in payload and ">" not in payload
    assert _json.loads(payload)["s"] == "</script><script>alert(1)</script>"
