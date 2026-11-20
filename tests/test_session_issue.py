from __future__ import annotations

import asyncio
import os
import re
import sys

import pytest

from projectmem.models import Event
from projectmem.storage import (
    OPEN_ISSUES_LISTED,
    ProjectMemError,
    initialize,
    open_issue_ids,
    sole_open_issue,
)


def _issue(issue_id: str) -> Event:
    return Event(type="issue", issue_id=issue_id, summary=f"bug {issue_id}")


def _fix(issue_id: str) -> Event:
    return Event(type="fix", issue_id=issue_id, summary=f"fixed {issue_id}")


def test_open_issue_ids_are_newest_first_without_fixed():
    events = [_issue("0001"), _issue("0002"), _issue("0003"), _fix("0002")]

    assert open_issue_ids(events) == ["0003", "0001"]


def test_sole_open_issue_resolves_the_only_open_issue():
    assert sole_open_issue([_issue("0001"), _issue("0002"), _fix("0001")]) == "0002"


def test_sole_open_issue_is_none_without_open_issues():
    assert sole_open_issue([_issue("0001"), _fix("0001")]) is None


def test_sole_open_issue_refuses_and_lists_every_open_issue():
    with pytest.raises(ProjectMemError, match=r"issue_id.*#0002, #0001"):
        sole_open_issue([_issue("0001"), _issue("0002")])


def test_refusal_caps_the_listing():
    count = OPEN_ISSUES_LISTED + 3
    events = [_issue(f"{i:04d}") for i in range(1, count + 1)]

    with pytest.raises(ProjectMemError, match="and 3 more"):
        sole_open_issue(events)


def _client_params(root):
    from mcp import StdioServerParameters

    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "projectmem.mcp_server"],
        env={
            "PROJECTMEM_ROOT": str(root),
            "PROJECTMEM_HOME": os.environ["PROJECTMEM_HOME"],
        },
    )


async def _text_of(client, name, arguments):
    from mcp.types import TextContent

    result = await client.call_tool(name, arguments)
    return "".join(b.text for b in result.content if isinstance(b, TextContent))


def test_two_mcp_processes_do_not_misfile(tmp_path):
    from mcp import Client

    initialize(tmp_path)
    params = _client_params(tmp_path)

    async def scenario():
        async with Client(params) as first, Client(params) as second:
            own = await _text_of(first, "log_issue", {"summary": "first session bug"})
            await _text_of(second, "log_issue", {"summary": "second session bug"})
            refused = await _text_of(
                first, "record_attempt", {"summary": "tried it", "outcome": "failed"}
            )
            own_id = re.search(r"#(\d+)", own).group(1)
            explicit = await _text_of(
                first,
                "record_attempt",
                {"summary": "tried it", "outcome": "failed", "issue_id": own_id},
            )
            return own_id, refused, explicit

    own_id, refused, explicit = asyncio.run(scenario())

    assert not refused.startswith("Recorded")
    assert "issue_id" in refused
    assert explicit.startswith(f"Recorded failed attempt on #{own_id}")


def test_one_open_issue_still_needs_no_id(tmp_path):
    from mcp import Client

    initialize(tmp_path)

    async def scenario():
        async with Client(_client_params(tmp_path)) as client:
            logged = await _text_of(client, "log_issue", {"summary": "lonely bug"})
            attempt = await _text_of(
                client, "record_attempt", {"summary": "tried it", "outcome": "failed"}
            )
            fixed = await _text_of(client, "record_fix", {"summary": "fixed it"})
            return re.search(r"#(\d+)", logged).group(1), attempt, fixed

    issue_id, attempt, fixed = asyncio.run(scenario())

    assert attempt.startswith(f"Recorded failed attempt on #{issue_id}")
    assert fixed.startswith(f"Fixed issue #{issue_id}")
