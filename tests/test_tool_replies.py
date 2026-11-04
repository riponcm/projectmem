from __future__ import annotations

import pytest

from projectmem import mcp_server
from projectmem.models import Event
from projectmem.storage import RECORD_CHARS, append_event, initialize, read_events

LONG_TEXT = "gotcha: " + " ".join(f"word{i}" for i in range(120))


@pytest.fixture
def root(tmp_path, monkeypatch):
    initialize(tmp_path)
    monkeypatch.setattr(mcp_server, "_PROJECT_ROOT", tmp_path)
    return tmp_path


def _newest_id(root) -> str:
    return read_events(root)[-1].id


def _name() -> str:
    return mcp_server._target(None).name


def test_add_note_reply_names_the_event_instead_of_echoing_it(root):
    reply = mcp_server.add_note(LONG_TEXT)

    assert reply == f"Recorded note {_newest_id(root)} → {_name()}"


def test_add_decision_reply_names_the_event_and_what_it_retires(root):
    first = mcp_server.add_decision("use one queue per device")
    old_id = _newest_id(root)
    reply = mcp_server.add_decision(LONG_TEXT, supersedes=old_id)

    assert first == f"Recorded decision {old_id} → {_name()}"
    assert reply == f"Recorded decision {_newest_id(root)} → {_name()} (supersedes {old_id})"


def test_issue_writes_reply_with_the_issue_id_only(root):
    logged = mcp_server.log_issue(LONG_TEXT[:100])
    attempted = mcp_server.record_attempt(LONG_TEXT, outcome="failed", issue_id="0001")
    fixed = mcp_server.record_fix(LONG_TEXT, issue_id="0001")

    assert logged == f"Logged issue #0001 → {_name()}"
    assert attempted == f"Recorded failed attempt on #0001 → {_name()}"
    assert fixed == f"Fixed issue #0001 → {_name()}"


def test_search_clips_long_matches_and_names_each_event(root):
    append_event(Event(type="note", summary=LONG_TEXT, location="src/queue.py"), root)
    event = read_events(root)[-1]

    reply = mcp_server.search_events("gotcha")
    line = next(line for line in reply.splitlines() if event.id in line)

    assert "..." in line
    assert "word119" not in line
    assert len(line) < RECORD_CHARS + 120
    assert event.timestamp[:10] in line
    assert "@ src/queue.py" in line


def test_search_full_returns_whole_records(root):
    append_event(Event(type="note", summary=LONG_TEXT), root)

    assert "word119" in mcp_server.search_events("gotcha", full=True)
