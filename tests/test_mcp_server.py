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

from projectmem.models import Event
from projectmem.storage import append_event, initialize


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
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
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
