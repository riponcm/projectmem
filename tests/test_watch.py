from __future__ import annotations

import os
import types
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from projectmem.cli import app
from projectmem.commands import watch as watch_command


def test_watch_status_not_running(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(app, ["init", "--no-watch"], catch_exceptions=False)

    result = runner.invoke(app, ["watch", "--status"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "not running" in result.stdout


def test_watch_stop_not_running(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(app, ["init", "--no-watch"], catch_exceptions=False)

    result = runner.invoke(app, ["watch", "--stop"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "No watcher running" in result.stdout


def test_watch_daemon_spawns_subprocess(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(app, ["init", "--no-watch"], catch_exceptions=False)

    fake_proc = MagicMock()
    fake_proc.pid = 4242
    fake_proc.poll.return_value = None

    with patch("subprocess.Popen", return_value=fake_proc) as mock_popen, \
         patch("time.sleep"):
        result = runner.invoke(app, ["watch", "--daemon"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "Watcher started" in result.stdout
        assert mock_popen.called
        cmd = mock_popen.call_args[0][0]
        assert "watch" in cmd
        assert "--worker" in cmd
        pid_file = tmp_path / ".projectmem" / "watch.pid"
        assert pid_file.is_file()
        assert pid_file.read_text(encoding="utf-8").strip() == "4242"


# ── Windows process liveness (regression for the orphaned-daemon bug) ────────
#
# `_running_pid` used `os.kill(pid, 0)`. On Windows `os.kill` routes to
# TerminateProcess, so signal 0 is not a liveness probe — it failed for live
# processes too. `--status` reported "not running", `--stop` found nothing, and
# `_running_pid` deleted the PID file on the way past, orphaning the worker and
# letting the next `--daemon` start another one.
#
# The Windows branch is exercised through a fake kernel32 because CI here is
# POSIX; what is asserted is the decision logic, not ctypes itself.

class _FakeKernel32:
    """Minimal stand-in for the four Win32 calls _pid_alive uses."""

    def __init__(self, *, alive_pids=(), denied_pids=()):
        self.alive = set(alive_pids)
        self.denied = set(denied_pids)
        self.last_error = 0
        self.terminated: list[int] = []
        self._handles: dict[int, int] = {}
        self._next = 100

    def OpenProcess(self, access, inherit, pid):  # noqa: N802
        if pid in self.denied:
            self.last_error = 5  # ERROR_ACCESS_DENIED
            return 0
        if pid not in self.alive:
            self.last_error = 87  # ERROR_INVALID_PARAMETER — what a dead pid gives
            return 0
        self._next += 1
        self._handles[self._next] = pid
        return self._next

    def GetLastError(self):  # noqa: N802
        return self.last_error

    def GetExitCodeProcess(self, handle, out):  # noqa: N802
        out._obj.value = 259 if self._handles.get(handle) in self.alive else 0
        return 1

    def TerminateProcess(self, handle, code):  # noqa: N802
        self.terminated.append(self._handles[handle])
        return 1

    def CloseHandle(self, handle):  # noqa: N802
        self._handles.pop(handle, None)
        return 1


def _as_windows(monkeypatch, fake):
    import ctypes

    monkeypatch.setattr(watch_command.sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(kernel32=fake),
                        raising=False)

    real_c_ulong = ctypes.c_ulong

    class _Box:
        def __init__(self, value=0):
            self.value = value

    def _byref(obj):
        return types.SimpleNamespace(_obj=obj)

    monkeypatch.setattr(ctypes, "c_ulong", _Box)
    monkeypatch.setattr(ctypes, "byref", _byref)
    return real_c_ulong


def test_pid_alive_sees_a_running_process_on_windows(monkeypatch):
    fake = _FakeKernel32(alive_pids=[4242])
    _as_windows(monkeypatch, fake)
    assert watch_command._pid_alive(4242) is True


def test_pid_alive_reports_dead_for_a_missing_process_on_windows(monkeypatch):
    fake = _FakeKernel32(alive_pids=[4242])
    _as_windows(monkeypatch, fake)
    assert watch_command._pid_alive(9999) is False


def test_pid_alive_treats_access_denied_as_alive(monkeypatch):
    """A process we cannot open still exists — calling it dead orphans it."""
    fake = _FakeKernel32(denied_pids=[777])
    _as_windows(monkeypatch, fake)
    assert watch_command._pid_alive(777) is True


def test_running_pid_keeps_the_pid_file_for_a_live_daemon_on_windows(
    tmp_path, monkeypatch
):
    """The regression. The file used to be deleted while the worker ran on."""
    mem = tmp_path / ".projectmem"
    mem.mkdir()
    (mem / "watch.pid").write_text("4242", encoding="utf-8")

    fake = _FakeKernel32(alive_pids=[4242])
    _as_windows(monkeypatch, fake)

    assert watch_command._running_pid(tmp_path) == 4242
    assert (mem / "watch.pid").exists(), "live daemon's PID file must survive"


def test_running_pid_cleans_up_a_genuinely_stale_pid_file(tmp_path, monkeypatch):
    mem = tmp_path / ".projectmem"
    mem.mkdir()
    (mem / "watch.pid").write_text("9999", encoding="utf-8")

    fake = _FakeKernel32(alive_pids=[4242])
    _as_windows(monkeypatch, fake)

    assert watch_command._running_pid(tmp_path) is None
    assert not (mem / "watch.pid").exists()


def test_terminate_uses_terminateprocess_on_windows(monkeypatch):
    fake = _FakeKernel32(alive_pids=[4242])
    _as_windows(monkeypatch, fake)
    watch_command._terminate(4242)
    assert fake.terminated == [4242]


def test_terminate_raises_process_lookup_for_a_missing_pid(monkeypatch):
    """_stop_daemon already handles this error; the Windows path must speak it."""
    fake = _FakeKernel32(alive_pids=[])
    _as_windows(monkeypatch, fake)
    with pytest.raises(ProcessLookupError):
        watch_command._terminate(4242)


def test_pid_alive_on_posix_reports_this_process(monkeypatch):
    monkeypatch.setattr(watch_command.sys, "platform", "darwin")
    assert watch_command._pid_alive(os.getpid()) is True
