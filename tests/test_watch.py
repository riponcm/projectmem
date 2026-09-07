from __future__ import annotations

from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from projectmem.cli import app


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
