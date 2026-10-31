from __future__ import annotations

import pytest
from typer.testing import CliRunner

from projectmem.cli import app


def test_summary_and_issue_file_are_regenerated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    runner.invoke(app, ["init"], catch_exceptions=False)
    runner.invoke(app, ["log", "login redirect loop"], catch_exceptions=False)
    runner.invoke(
        app,
        ["attempt", "changed redirect in auth/redirect.py:88", "--worked"],
        catch_exceptions=False,
    )
    runner.invoke(
        app,
        ["fix", "fixed redirect guard in auth/redirect.py:88"],
        catch_exceptions=False,
    )
    runner.invoke(
        app,
        ["decision", "Keep redirect handling in one middleware"],
        catch_exceptions=False,
    )

    summary = (tmp_path / ".projectmem" / "summary.md").read_text(encoding="utf-8")
    issue_files = list((tmp_path / ".projectmem" / "issues").glob("0001-*.md"))

    assert "# projectmem" in summary
    assert "[DONE] #0001 login redirect loop" in summary
    assert "Keep redirect handling in one middleware" in summary
    assert "`auth/redirect.py:88`" in summary
    assert len(issue_files) == 1
    issue_file = issue_files[0].read_text(encoding="utf-8")
    assert "login redirect loop" in issue_file
    assert "fixed redirect guard in auth/redirect.py:88" in issue_file


def test_regenerate_preserves_project_purpose(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    runner.invoke(app, ["init"], catch_exceptions=False)
    summary_path = tmp_path / ".projectmem" / "summary.md"
    summary = summary_path.read_text(encoding="utf-8")
    summary_path.write_text(
        summary.replace(
            "Replace this placeholder with a concise description of what this project "
            "does, who it serves, and the main technologies or runtime assumptions.",
            "A prediction ML project for evaluating demand forecasts.",
        ),
        encoding="utf-8",
    )

    runner.invoke(app, ["note", "models live in src/models"], catch_exceptions=False)

    regenerated = summary_path.read_text(encoding="utf-8")
    assert "A prediction ML project for evaluating demand forecasts." in regenerated
    assert "models live in src/models" in regenerated


def test_project_purpose_auto_syncs_from_project_map(tmp_path, monkeypatch):
    """L-037: when PROJECT_MAP.md has a real Project purpose and summary.md
    is still placeholder, regeneration should pull the purpose from
    PROJECT_MAP.md instead of echoing the placeholder back into summary.md."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    runner.invoke(app, ["init"], catch_exceptions=False)

    # Simulate the Setup Mode flow: AI writes a real Project purpose into
    # PROJECT_MAP.md (allowed — PROJECT_MAP.md is hand-editable) but
    # summary.md still has the init placeholder.
    map_path = tmp_path / ".projectmem" / "PROJECT_MAP.md"
    map_path.write_text(
        "# Project Map - test\n\n"
        "Status: created\n\n"
        "## Project purpose\n"
        "A neural network framework for academic research on graph "
        "convolutional networks (GCN) and attention mechanisms.\n\n"
        "## Main folders\n"
        "- `src/` — core library\n",
        encoding="utf-8",
    )

    # Any write tool triggers regenerate_summary. Use add_note (lightest).
    result = runner.invoke(
        app, ["note", "uses PyTorch for tensor ops"], catch_exceptions=False
    )
    assert result.exit_code == 0

    summary = (tmp_path / ".projectmem" / "summary.md").read_text(encoding="utf-8")

    # Real Project purpose from PROJECT_MAP.md now in summary.md
    assert "neural network framework for academic research" in summary
    # Placeholder is gone
    assert "Replace this placeholder" not in summary


def test_project_purpose_falls_back_when_project_map_is_placeholder(tmp_path, monkeypatch):
    """L-037: if PROJECT_MAP.md is still placeholder too, fall back to
    whatever summary.md already had (or the default placeholder)."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    runner.invoke(app, ["init"], catch_exceptions=False)
    # Both files still have init placeholders.

    runner.invoke(app, ["note", "first note"], catch_exceptions=False)

    summary = (tmp_path / ".projectmem" / "summary.md").read_text(encoding="utf-8")
    # Should remain placeholder — neither file has real Project purpose yet
    assert "Replace this placeholder" in summary
    assert "first note" in summary


def _issue_events(count: int, *, fixed: bool = False, first: int = 1):
    """N synthetic issues with ids first..first+count-1, optionally each fixed."""
    from projectmem.models import Event

    events = []
    for i in range(first, first + count):
        events.append(Event(type="issue", summary=f"issue {i:04d}", issue_id=f"{i:04d}"))
        if fixed:
            events.append(Event(type="fix", summary=f"fixed {i:04d}", issue_id=f"{i:04d}"))
    return events


def _recent_issue_lines(summary: str) -> list[str]:
    section = summary.split("## Recent issues")[1].split("## Decisions")[0]
    return [line for line in section.splitlines() if line.startswith("- [")]


def _section(summary: str, name: str) -> str:
    return summary.split(f"## {name}")[1].split("\n## ")[0]


def test_recent_issues_capped_at_ten(tmp_path):
    """Issue #19: Recent issues listed every issue ever logged (22 in the
    reporter's project), while Notes is capped at the latest 10. Fixed issues
    are capped at the 10 most recent so the summary stays a scannable snapshot."""
    from projectmem.summary import build_summary

    summary = build_summary(_issue_events(12, fixed=True), tmp_path)
    lines = _recent_issue_lines(summary)

    assert len(lines) == 10
    # The 10 most recent issues (highest ids) are kept...
    assert "#0012" in lines[0]
    assert "#0003" in lines[-1]
    # ...the two oldest fall out, and the summary says so.
    assert "#0001" not in summary
    assert "#0002" not in summary
    assert "2 older fixed issues not shown" in summary


def test_recent_issues_under_ten_all_listed(tmp_path):
    from projectmem.summary import build_summary

    summary = build_summary(_issue_events(3), tmp_path)
    lines = _recent_issue_lines(summary)

    assert len(lines) == 3
    assert "#0003" in lines[0]
    assert "#0001" in lines[-1]
    assert "not shown" not in summary


def test_an_old_open_issue_is_never_capped_away(tmp_path):
    """The cap must not hide what is still broken: #0001 is open and older
    than twelve fixed issues, and it is the line the next session needs."""
    from projectmem.summary import build_summary

    events = _issue_events(1) + _issue_events(12, fixed=True, first=2)
    lines = _recent_issue_lines(build_summary(events, tmp_path))

    assert lines[0].startswith("- [OPEN] #0001")
    assert sum(line.startswith("- [DONE]") for line in lines) == 10


def test_many_open_issues_are_all_listed(tmp_path):
    from projectmem.summary import build_summary

    lines = _recent_issue_lines(build_summary(_issue_events(15), tmp_path))

    assert len(lines) == 15


def test_a_large_open_backlog_is_named_not_dropped(tmp_path):
    """Real projects reach 30–40 open issues. Past 20 they are listed by id
    on one line: still visible, but they cannot regrow the summary."""
    from projectmem.summary import build_summary

    summary = build_summary(_issue_events(41), tmp_path)
    lines = _recent_issue_lines(summary)

    full = [line for line in lines if " older open issue" not in line]
    assert len(full) == 20
    assert "#0041" in full[0] and "#0022" in full[-1]
    overflow = next(line for line in lines if " older open issue" in line)
    assert overflow.startswith("- [OPEN] 21 older open issues, by id: #0021, #0020")
    for i in range(1, 42):
        assert f"#{i:04d}" in summary


def test_issue_ids_past_9999_order_numerically(tmp_path):
    """As strings "10000" < "9999", so the cap would have kept the old ids
    and dropped the newest ones."""
    from projectmem.summary import build_summary

    events = _issue_events(12, fixed=True, first=9995)  # 9995..10006
    lines = _recent_issue_lines(build_summary(events, tmp_path))

    assert "#10006" in lines[0]
    assert "#9997" in lines[-1]
    assert "#9995" not in "\n".join(lines)


def test_decisions_capped_at_newest_fifteen(tmp_path):
    from projectmem.models import Event
    from projectmem.summary import build_summary

    events = [Event(type="decision", summary=f"decision {i:02d}") for i in range(1, 21)]
    section = _section(build_summary(events, tmp_path), "Decisions")
    shown = [line for line in section.splitlines() if line.startswith("- decision")]

    assert len(shown) == 15
    # Newest kept, still in the order they were made.
    assert shown[0] == "- decision 06"
    assert shown[-1] == "- decision 20"
    assert "5 earlier decisions not shown" in section


def test_an_orphan_attempt_does_not_crash_the_summary(tmp_path):
    """An attempt whose issue event is missing (a typo id from an older
    version, or a hand-edited log) used to raise StopIteration on every
    rebuild, which broke every later write."""
    from projectmem.models import Event
    from projectmem.summary import build_summary

    events = _issue_events(1) + [
        Event(type="attempt", summary="tried x", issue_id="0099", outcome="failed")
    ]
    lines = _recent_issue_lines(build_summary(events, tmp_path))

    assert len(lines) == 1 and "#0001" in lines[0]



@pytest.mark.parametrize("issue_location,fix_location,shown", [
    ("auth/redirect.py", "auth/middleware.py", " [auth/redirect.py]"),
    (None, "auth/middleware.py", " [auth/middleware.py]"),
    (None, None, ""),
])
def test_a_fixed_issue_is_listed_by_its_title_alone(
    tmp_path, issue_location, fix_location, shown
):
    from projectmem.models import Event
    from projectmem.summary import build_summary

    events = [
        Event(type="issue", summary="login redirect loop", issue_id="0001",
              location=issue_location),
        Event(type="attempt", summary="cleared the session cookie first",
              issue_id="0001", outcome="failed"),
        Event(type="fix", summary="guarded the redirect in one middleware",
              issue_id="0001", location=fix_location),
    ]
    summary = build_summary(events, tmp_path)

    assert _recent_issue_lines(summary) == [
        f"- [DONE] #0001 login redirect loop{shown} (fixed)"
    ]
    assert "cleared the session cookie first" not in summary
    assert "guarded the redirect in one middleware" not in summary


def test_long_records_are_clipped_but_keep_their_location(tmp_path):
    from projectmem.models import Event
    from projectmem.storage import RECORD_CHARS
    from projectmem.summary import build_summary

    long_text = "the collector drops its websocket " * 30
    events = [
        Event(type="issue", summary=long_text, issue_id="0001",
              location="src/collector/socket.py"),
        Event(type="attempt", summary=long_text, issue_id="0001", outcome="partial"),
        Event(type="attempt", summary="warmed the cache in main.py",
              issue_id="0001", outcome="failed"),
        Event(type="decision", summary=long_text),
        Event(type="note", summary=long_text),
    ]
    summary = build_summary(events, tmp_path)

    issue_line = _recent_issue_lines(summary)[0]
    assert issue_line.endswith("... [src/collector/socket.py] (open)")
    assert "  - Failed attempt: warmed the cache in main.py\n" in summary
    clipped = [line for line in summary.splitlines() if "the collector drops" in line]
    assert len(clipped) == 4
    for line in clipped:
        kept = line[line.index("the collector drops"):].split("...")[0]
        assert long_text.startswith(kept)
        assert long_text[len(kept)] == " ", "clipped in the middle of a word"
        assert RECORD_CHARS // 2 < len(kept + "...") <= RECORD_CHARS


def test_a_long_path_is_not_clipped_down_to_the_word_before_it(tmp_path):
    from projectmem.models import Event
    from projectmem.storage import RECORD_CHARS
    from projectmem.summary import build_summary

    path = "src/" + "generated/" * 40 + "client.py"
    events = [Event(type="note", summary=f"see {path}")]
    note = _section(build_summary(events, tmp_path), "Notes").strip()

    assert note.startswith("- see src/generated/")
    assert note.endswith("...")
    assert len(note) == len("- ") + RECORD_CHARS


def test_a_multi_line_record_stays_on_one_summary_line(tmp_path):
    from projectmem.models import Event
    from projectmem.summary import build_summary

    events = [Event(type="note", summary="  first line\n## not a heading\n\tthird  ",
                    location="a.py\n## not a heading either")]
    summary = build_summary(events, tmp_path)

    assert "- first line ## not a heading third [a.py ## not a heading either]" in summary
    assert "\n## not a heading" not in summary
