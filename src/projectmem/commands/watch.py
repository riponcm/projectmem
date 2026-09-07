"""Real-time file watcher for projectmem.

Lean, opt-in daemon that detects file churn (rapid edits to the same file)
and logs them as auto-captured events. Battery-friendly: idles when no
activity, gitignore-aware, single-instance via PID file.

Usage:
    pjm watch                # foreground (Ctrl+C to stop)
    pjm watch --daemon       # background
    pjm watch --stop         # kill the daemon
    pjm watch --status       # is it running?
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Deque

import typer

from projectmem.models import Event
from projectmem.storage import (
    MEM_DIR,
    append_event,
    mem_path,
    require_mem_dir,
)


# ── Tunables (defaults — overridable via .projectmem/config.toml later) ──
CHURN_THRESHOLD = 4              # edits before logging churn
CHURN_WINDOW_SECONDS = 600       # 10-minute rolling window
DEBOUNCE_SECONDS = 0.5           # rapid saves within this count as one
IDLE_SLEEP_SECONDS = 30          # how long to sleep when nothing happens
ACTIVE_POLL_SECONDS = 2          # how often to scan while active
RECENT_LOG_COOLDOWN = 1800       # don't re-log same file's churn within 30 min

# Files / directories we always ignore (in addition to .gitignore)
ALWAYS_IGNORE = {
    ".git", ".projectmem", "node_modules", "__pycache__", ".venv", "venv",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build",
    ".next", ".nuxt", "target", ".idea", ".vscode", ".DS_Store",
    "*.pyc", "*.pyo", "*.log", "*.swp", "*.tmp",
}


def _pid_path(root: Path | None = None) -> Path:
    return mem_path(root) / "watch.pid"


def _log_path(root: Path | None = None) -> Path:
    return mem_path(root) / "watch.log"


def _read_gitignore(root: Path) -> set[str]:
    """Return additional ignore patterns from .gitignore (simple parsing)."""
    gi = root / ".gitignore"
    patterns: set[str] = set()
    if gi.exists():
        for line in gi.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                patterns.add(line.rstrip("/"))
    return patterns


def _should_ignore(path: Path, root: Path, gitignore: set[str]) -> bool:
    """Check if a file path should be ignored."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        return True
    parts = rel.parts
    if not parts:
        return True
    name = parts[-1]
    # Skip dotfiles in root unless explicitly tracked
    if any(p.startswith(".") and p not in (".gitignore", ".env.example") for p in parts):
        return True
    # ALWAYS_IGNORE
    for p in parts:
        if p in ALWAYS_IGNORE:
            return True
    # Suffix-based ignore
    for ig in ALWAYS_IGNORE:
        if ig.startswith("*.") and name.endswith(ig[1:]):
            return True
    # gitignore (simple substring match, not full gitignore semantics)
    for pat in gitignore:
        if pat in str(rel):
            return True
    return False


def _running_pid(root: Path | None = None) -> int | None:
    """Return PID if a daemon is running and reachable, else None."""
    pf = _pid_path(root)
    if not pf.exists():
        return None
    try:
        pid = int(pf.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None
    # Is process alive?
    try:
        os.kill(pid, 0)
        return pid
    except OSError:
        # Stale PID file — clean up
        try:
            pf.unlink()
        except OSError:
            pass
        return None


def _cleanup_pid_file(root: Path | None = None) -> None:
    pf = _pid_path(root)
    try:
        if pf.exists():
            pf.unlink()
    except OSError:
        pass


def run(
    daemon: bool = False,
    stop: bool = False,
    status: bool = False,
    worker: bool = False,
    root: Path | None = None,
) -> None:
    """Dispatch watch subcommands."""
    require_mem_dir(root)

    if stop:
        _stop_daemon(root)
        return
    if status:
        _show_status(root)
        return
    if worker:
        _run_worker(root)
        return

    # Check for existing daemon
    existing = _running_pid(root)
    if existing is not None:
        typer.echo(
            f"\033[33mprojectmem:\033[0m Watcher already running (PID {existing}).\n"
            f"  Stop with: pjm watch --stop"
        )
        return

    if daemon:
        _run_as_daemon(root)
    else:
        _run_foreground(root)


def _run_foreground(root: Path | None = None) -> None:
    """Run watcher in foreground (Ctrl+C to stop)."""
    typer.echo("\033[36mprojectmem:\033[0m Watcher started (foreground)")
    typer.echo("  Press Ctrl+C to stop.\n")
    _write_pid(os.getpid(), root)
    try:
        _watch_loop(root, verbose=True)
    except KeyboardInterrupt:
        typer.echo("\n\033[36mprojectmem:\033[0m Watcher stopped.")
    finally:
        _cleanup_pid_file(root)


def _run_worker(root: Path | None = None) -> None:
    """Run watch loop as background worker."""
    root_path = root or Path.cwd()
    _write_pid(os.getpid(), root_path)

    # Register signal handlers
    def _shutdown(signum, frame):
        print(f"[watch] received signal {signum}, shutting down")
        _cleanup_pid_file(root_path)
        sys.exit(0)

    try:
        signal.signal(signal.SIGTERM, _shutdown)
    except (ValueError, AttributeError):
        pass
    try:
        signal.signal(signal.SIGINT, _shutdown)
    except (ValueError, AttributeError):
        pass

    try:
        _watch_loop(root_path, verbose=False)
    finally:
        _cleanup_pid_file(root_path)


def _run_as_daemon(root: Path | None = None) -> None:
    """Spawn detached background process and run watch loop."""
    root_path = (root or Path.cwd()).resolve()
    log_file = _log_path(root_path)

    try:
        log_fd = open(log_file, "a", encoding="utf-8")
    except OSError as e:
        typer.echo(f"\033[31mprojectmem:\033[0m Cannot open log file: {e}", err=True)
        return

    cmd = [sys.executable, "-m", "projectmem.cli", "watch", "--worker"]

    try:
        if sys.platform.startswith("win"):
            flags = (
                subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP
                | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            )
            proc = subprocess.Popen(
                cmd,
                cwd=str(root_path),
                stdin=subprocess.DEVNULL,
                stdout=log_fd,
                stderr=log_fd,
                creationflags=flags,
                close_fds=True,
            )
        else:
            proc = subprocess.Popen(
                cmd,
                cwd=str(root_path),
                stdin=subprocess.DEVNULL,
                stdout=log_fd,
                stderr=log_fd,
                start_new_session=True,
                close_fds=True,
            )
    except OSError as e:
        typer.echo(f"\033[31mprojectmem:\033[0m Daemon spawn failed: {e}", err=True)
        return
    finally:
        log_fd.close()

    # Give child a moment to write PID and initialize
    time.sleep(0.3)
    if proc.poll() is not None:
        typer.echo(
            f"\033[31mprojectmem:\033[0m Daemon exited unexpectedly (code {proc.returncode}) — check .projectmem/watch.log",
            err=True,
        )
        return

    new_pid = _running_pid(root_path) or proc.pid
    _write_pid(new_pid, root_path)
    typer.echo(
        f"\033[32mprojectmem:\033[0m Watcher started (PID {new_pid})\n"
        f"  Logs: .projectmem/watch.log\n"
        f"  Stop: pjm watch --stop"
    )


def _write_pid(pid: int, root: Path | None = None) -> None:
    pf = _pid_path(root)
    pf.write_text(str(pid), encoding="utf-8")


def _watch_loop(root: Path | None, verbose: bool = False) -> None:
    """Core file-watching loop using watchdog."""
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler

    root_path = root or Path.cwd()
    gitignore = _read_gitignore(root_path)

    # Per-file edit history (timestamps within the rolling window)
    edits: dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=50))
    # Last-debounce timestamp per file
    last_seen: dict[str, float] = {}
    # Cooldown so we don't re-log the same file every minute
    last_logged: dict[str, float] = {}

    def _on_change(path_str: str) -> None:
        path = Path(path_str)
        if _should_ignore(path, root_path, gitignore):
            return

        now = time.time()
        # Debounce
        if path_str in last_seen and (now - last_seen[path_str]) < DEBOUNCE_SECONDS:
            return
        last_seen[path_str] = now

        # Record the edit
        history = edits[path_str]
        history.append(now)
        # Trim to window
        while history and (now - history[0]) > CHURN_WINDOW_SECONDS:
            history.popleft()

        if verbose:
            try:
                rel = path.relative_to(root_path)
            except ValueError:
                rel = path
            print(f"[watch] edit: {rel} (count: {len(history)})", flush=True)

        # Threshold check + cooldown
        if len(history) >= CHURN_THRESHOLD:
            last_log = last_logged.get(path_str, 0)
            if (now - last_log) >= RECENT_LOG_COOLDOWN:
                _log_churn_event(path, len(history), root_path, verbose=verbose)
                last_logged[path_str] = now
                # Clear history so we don't log again until threshold reached fresh
                history.clear()

    class _Handler(FileSystemEventHandler):
        def on_modified(self, event):
            if not event.is_directory:
                _on_change(event.src_path)

        def on_created(self, event):
            if not event.is_directory:
                _on_change(event.src_path)

    observer = Observer()
    observer.schedule(_Handler(), str(root_path), recursive=True)
    observer.start()

    if verbose:
        print(f"[watch] watching {root_path} (threshold: {CHURN_THRESHOLD} edits / {CHURN_WINDOW_SECONDS}s)", flush=True)

    try:
        idle_count = 0
        while True:
            if any(edits.values()):
                time.sleep(ACTIVE_POLL_SECONDS)
                idle_count = 0
            else:
                idle_count += 1
                sleep_for = IDLE_SLEEP_SECONDS if idle_count > 3 else ACTIVE_POLL_SECONDS
                time.sleep(sleep_for)
            # Trim stale entries
            now = time.time()
            for path_str in list(edits.keys()):
                history = edits[path_str]
                while history and (now - history[0]) > CHURN_WINDOW_SECONDS:
                    history.popleft()
                if not history:
                    del edits[path_str]
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join(timeout=2)


def _log_churn_event(
    path: Path, edit_count: int, root: Path, verbose: bool = False
) -> None:
    """Append a churn event to events.jsonl (atomic, no locking needed)."""
    try:
        rel = str(path.relative_to(root))
    except ValueError:
        rel = str(path)

    summary = (
        f"High churn detected: {rel} ({edit_count} edits in "
        f"{CHURN_WINDOW_SECONDS // 60} min)"
    )

    event = Event(
        type="note",
        summary=summary,
        files=[rel],
        location=rel,
        auto_captured=True,
        capture_source="churn_detector",
        capture_confidence="medium",
        command="watch",
    )

    try:
        append_event(event, root)
        if verbose:
            print(f"[watch] LOGGED CHURN: {rel} ({edit_count} edits)", flush=True)
    except Exception as exc:
        if verbose:
            print(f"[watch] failed to write event: {exc}", flush=True)


def _stop_daemon(root: Path | None = None) -> None:
    """Send SIGTERM to the running daemon."""
    pid = _running_pid(root)
    if pid is None:
        typer.echo("\033[33mprojectmem:\033[0m No watcher running.")
        return

    try:
        os.kill(pid, signal.SIGTERM)
        # Wait up to 3s for clean exit
        for _ in range(15):
            time.sleep(0.2)
            if _running_pid(root) is None:
                break
        _cleanup_pid_file(root)
        typer.echo(f"\033[32mprojectmem:\033[0m Watcher stopped (PID {pid}).")
    except ProcessLookupError:
        _cleanup_pid_file(root)
        typer.echo("\033[33mprojectmem:\033[0m Watcher process not found (cleaned up stale PID).")
    except PermissionError:
        typer.echo(
            f"\033[31mprojectmem:\033[0m Permission denied to stop PID {pid}.",
            err=True,
        )


def _show_status(root: Path | None = None) -> None:
    """Print whether a daemon is running."""
    pid = _running_pid(root)
    if pid is None:
        typer.echo(
            "\033[2m○\033[0m \033[33mnot running\033[0m\n"
            "  Start with: pjm watch --daemon"
        )
        return

    # Try to get uptime
    pid_file = _pid_path(root)
    mtime = pid_file.stat().st_mtime if pid_file.exists() else None
    uptime_str = ""
    if mtime:
        seconds = int(time.time() - mtime)
        h, m = divmod(seconds // 60, 60)
        uptime_str = f"{h}h {m}m" if h else f"{m}m"

    typer.echo(
        f"\033[32m●\033[0m \033[32mrunning\033[0m · PID {pid}"
        + (f" · uptime {uptime_str}" if uptime_str else "")
    )
    typer.echo(f"  Logs: .projectmem/watch.log")
    typer.echo(f"  Stop: pjm watch --stop")
