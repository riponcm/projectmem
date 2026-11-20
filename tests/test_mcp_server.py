from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from conftest import set_fake_home
from projectmem.models import Event
from projectmem.storage import append_event, initialize
from projectmem.summary import regenerate_summary


def _completed(args: list[str], stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")


def test_git_helpers_detach_stdin_and_keep_timeouts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from projectmem import storage, staleness
    from projectmem.commands import context, precheck

    calls: list[dict[str, Any]] = []

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append({"args": args, **kwargs})
        if args[:2] == ["git", "status"]:
            return _completed(args, "XY README.md\n")
        if args[:2] == ["git", "rev-parse"]:
            return _completed(args, "abc123\n")
        return _completed(args, "abc msg\n")

    monkeypatch.setattr(storage.subprocess, "run", fake_run)
    monkeypatch.setattr(staleness.subprocess, "run", fake_run)
    monkeypatch.setattr(precheck.subprocess, "run", fake_run)
    monkeypatch.setattr(context.subprocess, "run", fake_run)

    assert storage.get_git_commit(tmp_path) == "abc123"
    assert (
        staleness.commits_touching_since(
            "README.md", "2026-01-01T00:00:00Z", tmp_path
        )
        == 1
    )
    assert precheck._git_recent_changes("README.md", 30, tmp_path) == 1
    assert precheck._get_staged_files(tmp_path) == ["abc msg"]
    assert precheck._get_working_tree_files(tmp_path) == ["abc msg"]
    assert context._get_git_status_files(tmp_path) == ["README.md"]

    assert calls
    assert all(call.get("stdin") is subprocess.DEVNULL for call in calls)
    assert all(call.get("timeout") == 5 for call in calls)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )


def _make_git_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    set_fake_home(monkeypatch, str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t.test")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base")
    initialize(repo)
    return repo


def _mcp_env(repo_root: Path, tmp_path: Path) -> dict[str, str]:
    src_path = str(repo_root / "src")
    existing = os.environ.get("PYTHONPATH")
    return {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "PYTHONPATH": src_path if not existing else src_path + os.pathsep + existing,
    }


def _call_mcp_tool(
    project: Path,
    tmp_path: Path,
    name: str,
    arguments: dict[str, Any],
    timeout: float = 6.0,
) -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parents[1]
    proc = subprocess.Popen(
        [sys.executable, "-m", "projectmem.mcp_server", "--root", str(project)],
        cwd=project,
        env=_mcp_env(repo_root, tmp_path),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    stdout_q: queue.Queue[str] = queue.Queue()

    def pump_stdout() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            stdout_q.put(line.rstrip("\n"))

    thread = threading.Thread(target=pump_stdout, daemon=True)
    thread.start()

    def send(message: dict[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    def read_response(
        message_id: int, wait_seconds: float
    ) -> dict[str, Any] | None:
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            try:
                line = stdout_q.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("id") == message_id:
                return payload
        return None

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "projectmem-test", "version": "0"},
                },
            }
        )
        initialized = read_response(1, timeout)
        assert initialized is not None
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        response = read_response(2, timeout)
        if response is None:
            pytest.fail(f"MCP tool {name} did not respond within {timeout}s")
        return response
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _tool_text(response: dict[str, Any]) -> str:
    content = response["result"]["content"]
    return "\n".join(item.get("text", "") for item in content)


def test_mcp_stdio_precheck_file_returns_with_git_backed_analysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _make_git_project(tmp_path, monkeypatch)
    append_event(
        Event(
            type="note",
            summary="README precheck regression fixture",
            location="README.md",
        ),
        project,
    )

    response = _call_mcp_tool(
        project,
        tmp_path,
        "precheck_file",
        {"file_path": "README.md"},
    )

    assert response["result"]["isError"] is False
    assert "README.md" in _tool_text(response)


def test_mcp_precheck_file_names_a_memory_file_instead_of_clearing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"no warnings. Safe to modify." on `.projectmem/summary.md` contradicted
    the instructions the same server hands out: memory files are regenerated
    from the log and are never edited by hand."""
    project = _make_git_project(tmp_path, monkeypatch)

    response = _call_mcp_tool(
        project, tmp_path, "precheck_file", {"file_path": ".projectmem/summary.md"},
    )

    assert response["result"]["isError"] is False
    text = _tool_text(response)
    assert "Safe to modify" not in text
    assert "memory file" in text and "add_note" in text


def test_mcp_stdio_add_note_returns_and_writes_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _make_git_project(tmp_path, monkeypatch)

    response = _call_mcp_tool(
        project,
        tmp_path,
        "add_note",
        {"summary": "MCP smoke note from pytest"},
    )

    assert response["result"]["isError"] is False
    assert "Recorded note" in _tool_text(response)
    events_text = (project / ".projectmem" / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert "MCP smoke note from pytest" in events_text


def test_mcp_server_instantiation():
    from projectmem.mcp_server import mcp

    assert mcp is not None
    assert mcp.name == "projectmem"


def test_server_builds_when_fastmcp_is_gone(monkeypatch, tmp_path):
    """mcp 2.x ships `mcp.server.fastmcp` as a module that raises on import.

    Load a fresh copy of mcp_server with that path poisoned and a stand-in for
    `mcp.server.mcpserver`, so the fallback is exercised even though the test
    environment has mcp 1.x installed.
    """
    import importlib.util
    import sys
    import types

    from projectmem import mcp_server as installed

    class _StubServer:
        def __init__(self, name=None, instructions=None, **kwargs):
            self.name = name
            self.tools = []

        def tool(self, *args, **kwargs):
            def decorate(fn):
                self.tools.append(fn.__name__)
                return fn
            return decorate

        def run(self, *args, **kwargs):  # pragma: no cover - never called here
            pass

    stub_module = types.ModuleType("mcp.server.mcpserver")
    stub_module.MCPServer = _StubServer
    monkeypatch.setitem(sys.modules, "mcp.server.mcpserver", stub_module)
    # None in sys.modules makes `from … import …` raise ImportError, which is
    # what mcp 2.x's tombstone module does.
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", None)

    spec = importlib.util.spec_from_file_location(
        "projectmem._mcp_server_v2_probe", installed.__file__
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert isinstance(module.mcp, _StubServer)
    assert module.mcp.name == "projectmem"
    # 15 memory tools + list_projects / current_project for global routing
    assert len(module.mcp.tools) == 18
    assert "list_projects" in module.mcp.tools


@pytest.mark.parametrize("spelling", ["src/auth.py", "./src/auth.py", "ABSOLUTE"])
def test_mcp_precheck_file_matches_every_spelling_of_a_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spelling: str
) -> None:
    """The field accepts "project-relative or absolute" paths, but only the
    exact stored string matched: `./src/auth.py` and the absolute path came
    back "Safe to modify" past three failed attempts and an open issue."""
    project = _make_git_project(tmp_path, monkeypatch)
    (project / "src").mkdir()
    (project / "src" / "auth.py").write_text("x = 1\n", encoding="utf-8")
    append_event(
        Event(type="issue", summary="token refresh race", location="src/auth.py",
              issue_id="0001"),
        project,
    )
    for i in range(3):
        append_event(
            Event(type="attempt", summary=f"lock variant {i}", outcome="failed",
                  location="src/auth.py"),
            project,
        )
    file_path = str(project / "src" / "auth.py") if spelling == "ABSOLUTE" else spelling

    text = _tool_text(
        _call_mcp_tool(project, tmp_path, "precheck_file", {"file_path": file_path})
    )

    assert "Safe to modify" not in text
    assert "3 attempts" in text


def test_mcp_precheck_file_matches_an_event_logged_with_an_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _make_git_project(tmp_path, monkeypatch)
    (project / "src").mkdir()
    (project / "src" / "db.py").write_text("x = 1\n", encoding="utf-8")
    append_event(
        Event(type="issue", summary="pool leak",
              location=str(project / "src" / "db.py"), issue_id="0001"),
        project,
    )

    text = _tool_text(
        _call_mcp_tool(project, tmp_path, "precheck_file", {"file_path": "src/db.py"})
    )

    assert "Safe to modify" not in text


def test_mcp_get_issue_accepts_unpadded_ids_and_rejects_globs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The field promises "Numeric strings without padding (e.g., '42') are
    also accepted", but `1` found nothing and `*` returned issue 0001."""
    project = _make_git_project(tmp_path, monkeypatch)
    _call_mcp_tool(project, tmp_path, "log_issue", {"summary": "login double submit"})

    for issue_id in ("0001", "1", "01", "#1"):
        text = _tool_text(
            _call_mcp_tool(project, tmp_path, "get_issue", {"issue_id": issue_id})
        )
        assert "login double submit" in text, issue_id

    for issue_id in ("*", "2"):
        text = _tool_text(
            _call_mcp_tool(project, tmp_path, "get_issue", {"issue_id": issue_id})
        )
        assert text == f"No issue found with ID {issue_id}.", issue_id


def test_mcp_get_issue_finds_backfilled_legacy_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`pjm init` backfill writes ids like `legacy_f436`, which `pjm fix`
    accepts; a digits-only check made get_issue refuse them."""
    project = _make_git_project(tmp_path, monkeypatch)
    append_event(
        Event(type="issue", issue_id="legacy_f436", summary="Legacy issue: fix login"),
        project,
    )
    regenerate_summary(project)

    text = _tool_text(
        _call_mcp_tool(project, tmp_path, "get_issue", {"issue_id": "legacy_f436"})
    )
    assert "fix login" in text

    for issue_id in ("legacy_*", "legacy_f43?", "[l]egacy_f436"):
        text = _tool_text(
            _call_mcp_tool(project, tmp_path, "get_issue", {"issue_id": issue_id})
        )
        assert text == f"No issue found with ID {issue_id}.", issue_id


def test_mcp_precheck_files_matches_single_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _make_git_project(tmp_path, monkeypatch)
    (project / "src").mkdir()
    for name in ("auth.py", "db.py"):
        (project / "src" / name).write_text("x = 1\n", encoding="utf-8")
    append_event(
        Event(type="issue", summary="token refresh race", location="src/auth.py",
              issue_id="0001"),
        project,
    )
    append_event(
        Event(type="attempt", summary="lock variant", outcome="failed",
              issue_id="0001"),
        project,
    )

    batch = _tool_text(_call_mcp_tool(
        project, tmp_path, "precheck_files", {"file_paths": ["src/auth.py", "src/db.py"]}
    ))
    single = _tool_text(_call_mcp_tool(
        project, tmp_path, "precheck_file", {"file_path": "src/auth.py"}
    ))

    assert "lock variant" in batch and "lock variant" in single
    assert "src/db.py: no warnings" in batch
