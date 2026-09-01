"""`pjm doctor` — check the setup, and offer to fix what it finds.

Upgrading used to leave three silent problems: projects with memory that were
never registered (the registry only exists since 0.2.0), MCP client configs
still pinned to one repo, and registry entries pointing at folders that are
gone. None of them raise an error — the tool just quietly does less than it
should.

There is no install-time hook to do this in: a wheel unpacks, it does not run
code. So this is a command, and the CLI points at it once after an upgrade.
Nothing is written without --fix.
"""
from __future__ import annotations

import os
import string
import sys
from pathlib import Path

import typer

from projectmem import __version__
from projectmem.commands.init import _pinned_client_configs
from projectmem.commands.project import _find_projects
from projectmem.project_registry import (
    RegistryError,
    load_registry,
    register,
    unregister,
)
from projectmem.storage import MEM_DIR

# Where people actually keep code. Scanned shallowly and only when asked.
_HOME_DIRS = [
    "Developer", "Documents", "Desktop", "code", "Code", "src", "source",
    "projects", "Projects", "repos", "repo", "work", "git", "dev",
]


# Cloud clients, by where each one actually puts its folder.
_CLOUD_PATTERNS = [
    "OneDrive*",                                   # personal and business, all OSes
    "Dropbox*",
    "Google Drive*", "GoogleDrive*", "gdrive",     # older clients and Linux mounts
    "Library/CloudStorage/*",                      # macOS: OneDrive, Google, Box, Dropbox
    "Library/Mobile Documents/com~apple~CloudDocs",  # iCloud Drive on macOS
    "iCloudDrive*",                                # iCloud on Windows
    "Nextcloud*", "ownCloud*",
    "Box", "Box Sync",
    "MEGA*", "pCloud*", "Sync",
    "Seafile*", "Yandex.Disk*",
]


def _windows_fixed_drives() -> list[Path]:
    """Fixed drives only.

    A mapped network drive that is offline can make a plain exists() check hang
    for a long time on a corporate laptop, so ask Windows what kind of drive
    each one is first. Falls back to a plain check if that is unavailable.
    """
    letters = [f"{c}:/" for c in string.ascii_uppercase]
    try:
        import ctypes

        DRIVE_FIXED = 3
        get_type = ctypes.windll.kernel32.GetDriveTypeW  # type: ignore[attr-defined]
        return [Path(d) for d in letters if get_type(d) == DRIVE_FIXED]
    except Exception:
        out = []
        for d in letters:
            try:
                if Path(d).exists():
                    out.append(Path(d))
            except OSError:
                continue
        return out


def dedupe_paths(paths: list[Path]) -> list[Path]:
    """Collapse paths that are the same directory.

    Not string comparison, and not resolve(): macOS is case-insensitive by
    default, so ~/code and ~/Code are one folder while resolve() keeps them
    distinct — which registered every project inside twice. (device, inode) is
    the only identity that survives case-folding, symlinks and cloud folders
    reachable under two names.
    """
    seen: set[tuple[int, int]] = set()
    unique: list[Path] = []
    for path in paths:
        try:
            if not path.is_dir():
                continue
            info = path.stat()
            key = (info.st_dev, info.st_ino)
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def default_roots() -> list[Path]:
    """Likely code locations for this machine.

    On Windows the fixed drives matter as much as the home directory: projects
    live on D:\\ and E:\\ as often as under %USERPROFILE%, and a user should not
    have to remember to scan each one.
    """
    home = Path.home()
    roots = [home / name for name in _HOME_DIRS]
    # Cloud-synced folders hold real work — on a managed Windows or Mac,
    # Documents and Desktop are often redirected into OneDrive wholesale.
    # Listing these is cheap even for online-only files: we stat directories,
    # never read file contents, so nothing is pulled down from the cloud.
    for pattern in _CLOUD_PATTERNS:
        try:
            roots.extend(sorted(home.glob(pattern)))
        except OSError:
            continue
    if sys.platform.startswith("win"):
        # C: is covered by the home-directory entries above; scanning it whole
        # would mean walking Windows and Program Files for nothing.
        roots.extend(d for d in _windows_fixed_drives() if d != Path("C:/"))
    return dedupe_paths(roots)


PYPI_JSON = "https://pypi.org/pypi/projectmem/json"
CHECK_INTERVAL_SECONDS = 24 * 3600


def _as_tuple(version: str) -> tuple[int, ...]:
    """Comparable form of x.y.z, ignoring any suffix. No new dependency."""
    import itertools

    parts = []
    for chunk in version.split(".")[:3]:
        # Leading digits only. Removing every non-digit turned "0rc1" into 01,
        # which made 1.0.0rc1 sort ABOVE 1.0.0 and would have told someone on a
        # final release that a release candidate was newer.
        digits = "".join(itertools.takewhile(str.isdigit, chunk))
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def latest_version(timeout: float = 3.0) -> str | None:
    """Ask PyPI what the newest release is. Returns None on any failure.

    The only network call projectmem ever makes, and it never happens on its
    own: either --online for a one-off check, or after the user turns the check
    on. Nothing about the machine is sent — it is a plain GET of a public JSON
    file, the same one `pip install` reads.
    """
    import json as _json
    import urllib.request

    try:
        request = urllib.request.Request(
            PYPI_JSON, headers={"User-Agent": f"projectmem/{__version__}"}
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return _json.load(response)["info"]["version"]
    except Exception:
        return None


def set_auto_check(enabled: bool) -> None:
    """Remember whether doctor may check PyPI on its own (default: no)."""
    from projectmem.project_registry import load_meta, save_meta

    meta = load_meta()
    meta["update_check"] = bool(enabled)
    save_meta(meta)


def _update_line(online: bool) -> None:
    """Report the installed version, and the newest one when asked to look."""
    from projectmem.project_registry import load_meta, save_meta

    meta = load_meta()
    enabled = bool(meta.get("update_check"))
    if not (online or enabled):
        typer.secho(f"\n  ✓ Running {__version__}", fg=typer.colors.GREEN)
        typer.echo("      Check PyPI for a newer release:  pjm doctor --online")
        typer.echo("      Check daily from now on:         pjm doctor --auto")
        typer.echo("      (projectmem makes no network calls unless you ask.)")
        return

    import time

    last = meta.get("update_checked_at") or 0
    newest = meta.get("update_latest")
    if online or (time.time() - float(last)) > CHECK_INTERVAL_SECONDS:
        fetched = latest_version()
        if fetched:
            newest = fetched
            meta["update_checked_at"] = time.time()
            meta["update_latest"] = fetched
            save_meta(meta)
    if not newest:
        typer.secho(f"\n  ✓ Running {__version__}", fg=typer.colors.GREEN)
        typer.echo("      Could not reach PyPI — nothing else was affected.")
        return
    if _as_tuple(newest) > _as_tuple(__version__):
        typer.secho(
            f"\n  ⚠ {newest} is available (you have {__version__})",
            fg=typer.colors.YELLOW,
        )
        typer.echo("      pip install -U projectmem")
    else:
        typer.secho(f"\n  ✓ Running {__version__} — the latest", fg=typer.colors.GREEN)


def run(
    fix: bool = False,
    depth: int = 4,
    roots: list[Path] | None = None,
    online: bool = False,
    auto: bool | None = None,
) -> None:
    """Report problems; with fix=True, resolve the ones that are safe to."""
    if auto is not None:
        set_auto_check(auto)
        typer.secho(
            f"✓ Automatic update checks {'on (once a day)' if auto else 'off'}\n",
            fg=typer.colors.GREEN,
        )
    scan_roots = [r.expanduser().resolve() for r in roots] if roots else default_roots()
    registry = load_registry()
    known = {r.path for r in registry.projects}
    problems = 0

    typer.echo(f"projectmem {__version__} — checking your setup\n")

    # ── 1. projects with memory that nobody registered ──
    typer.echo(f"Scanning {len(scan_roots)} location(s) for projects…")
    found: list[Path] = []
    for root in scan_roots:
        found.extend(_find_projects(root, depth))
    found = dedupe_paths(found)
    known_keys = set()
    for path in known:
        try:
            info = path.stat()
            known_keys.add((info.st_dev, info.st_ino))
        except OSError:
            continue
    missing = []
    for project in found:
        try:
            info = project.stat()
        except OSError:
            continue
        if (info.st_dev, info.st_ino) not in known_keys:
            missing.append(project)
    if missing:
        problems += 1
        typer.secho(
            f"  ⚠ {len(missing)} project(s) have memory but are not registered",
            fg=typer.colors.YELLOW,
        )
        for project in missing[:10]:
            typer.echo(f"      {project}")
        if len(missing) > 10:
            typer.echo(f"      … and {len(missing) - 10} more")
        if fix:
            for project in missing:
                try:
                    register(project)
                except RegistryError as exc:
                    typer.secho(f"      ✗ {project}: {exc}", fg=typer.colors.RED)
            typer.secho(f"  ✓ Registered {len(missing)}", fg=typer.colors.GREEN)
    else:
        typer.secho(f"  ✓ All {len(found)} project(s) found are registered", fg=typer.colors.GREEN)

    # ── 2. registry entries whose memory is gone ──
    stale = [r for r in registry.projects if not (r.path / MEM_DIR).is_dir()]
    if stale:
        problems += 1
        typer.secho(f"\n  ⚠ {len(stale)} registered project(s) no longer have memory", fg=typer.colors.YELLOW)
        for record in stale:
            typer.echo(f"      {record.name}  {record.path}")
        if fix:
            for record in stale:
                unregister(record.id)
            typer.secho(f"  ✓ Removed {len(stale)} from the registry (repos untouched)", fg=typer.colors.GREEN)
    else:
        typer.secho("\n  ✓ Every registered project still has its memory", fg=typer.colors.GREEN)

    # ── 3. client configs still pinned to one repo ──
    pinned = _pinned_client_configs()
    if pinned:
        problems += 1
        typer.secho("\n  ⚠ MCP client config(s) still pinned to a single repo", fg=typer.colors.YELLOW)
        for client, path in pinned:
            typer.echo(f"      {client}  {path}")
        typer.echo('      Remove --root / cwd / PROJECTMEM_ROOT from the projectmem entry.')
        typer.echo("      projectmem never edits client settings — that one is yours to change.")
    else:
        typer.secho("\n  ✓ No MCP client config is pinned to a single repo", fg=typer.colors.GREEN)

    _update_line(online)

    typer.echo("")
    if not problems:
        typer.secho("Everything checks out.", fg=typer.colors.GREEN, bold=True)
    elif fix:
        typer.secho("Fixed what could be fixed automatically.", fg=typer.colors.GREEN, bold=True)
    else:
        typer.secho("Run `pjm doctor --fix` to apply the fixes above.", bold=True)
