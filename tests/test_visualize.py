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
