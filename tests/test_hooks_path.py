"""Regression test for L-047 — the conda/venv hook silently-no-op bug.

Before the fix, ``install_hooks`` wrote hook snippets that relied on
``command -v pjm`` at runtime. Under conda/pyenv/venv the interactive
shell's PATH isn't inherited by git's non-interactive hook bash, so the
lookup returned nothing and the hook silently did nothing — including
the pre-commit warning, projectmem's headline feature.

The fix bakes the install-time absolute path to ``pjm`` into the hook,
with a runtime ``command -v`` fallback for the case where the binary
was moved. This test simulates a stripped-PATH non-interactive bash
(approximating git's hook environment) and confirms the hook still
finds and invokes ``pjm``.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from projectmem.commands.hooks import (
    HOOK_MARKER_END,
    HOOK_MARKER_START,
    _auto_capture_snippet,
    _precheck_snippet,
    _resolve_pjm_binary,
    install_hooks,
)


def test_resolve_pjm_binary_returns_absolute_path_or_bare_name() -> None:
    """The resolver must return *something* — never raise."""
    found = _resolve_pjm_binary()
    assert isinstance(found, str) and found
    # Either an absolute path (the normal case) or the literal "pjm" fallback.
    assert found == "pjm" or os.path.isabs(found)


def test_precheck_snippet_bakes_path_into_script(tmp_path: Path) -> None:
    snippet = _precheck_snippet("/opt/example/bin/pjm")
    assert 'PJM_BIN="/opt/example/bin/pjm"' in snippet
    # Markers preserved so uninstall still works.
    assert HOOK_MARKER_START in snippet
    assert HOOK_MARKER_END in snippet
    # The runtime fallback is there too.
    assert "command -v pjm" in snippet


def test_auto_capture_snippet_bakes_path_and_capture_arg() -> None:
    snippet = _auto_capture_snippet("/opt/example/bin/pjm", "commit")
    assert 'PJM_BIN="/opt/example/bin/pjm"' in snippet
    assert '"$PJM_BIN" _auto-capture "commit"' in snippet
    # No leftover `$1` placeholder.
    assert "$1" not in snippet
    # L-050: both stdout AND stderr must be silenced, or the
    # backgrounded capture output prints over the user's shell prompt
    # after `git commit` returns. The old `2>/dev/null` (stderr only)
    # form is forbidden.
    assert ">/dev/null 2>&1 &" in snippet
    assert "2>/dev/null &" not in snippet


@pytest.fixture
def fake_pjm(tmp_path: Path) -> Path:
    """Create a fake `pjm` binary that records its invocation arguments."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "pjm.log"
    pjm = bin_dir / "pjm"
    pjm.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{log_path}"\n'
        "exit 0\n"
    )
    os.chmod(pjm, 0o755)
    return pjm


def _require_launchable_bash() -> str:
    """Return a bash executable path, or skip when this host cannot run one."""
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is not available")

    try:
        result = subprocess.run(
            [bash, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"bash is not launchable: {exc}")

    if result.returncode != 0:
        reason = _bash_failure_reason(result)
        pytest.skip(f"bash is not launchable: {reason[:120]}")

    return bash


def _bash_failure_reason(result: subprocess.CompletedProcess[str]) -> str:
    """Normalize Windows bash/WSL startup errors for readable skip messages."""
    return (
        ((result.stderr or "") + "\n" + (result.stdout or ""))
        .replace("\x00", "")
        .strip()
    )


def test_hook_runs_under_stripped_path(tmp_path: Path, fake_pjm: Path) -> None:
    """Simulate git's non-interactive hook environment: PATH = /usr/bin only.

    The hook must still find pjm via the baked absolute path, NOT via
    `command -v`. This is the exact failure mode L-047 fixes.
    """
    bash = _require_launchable_bash()

    # Lay out a repo with .projectmem so the hook's directory check passes.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".projectmem").mkdir()

    # Build the snippet that install_hooks would write — but with our fake
    # pjm path baked in instead of the real one.
    snippet = _precheck_snippet(str(fake_pjm))
    hook = repo / "pre-commit"
    hook.write_text("#!/usr/bin/env bash\n" + snippet)
    os.chmod(hook, 0o755)

    # Strip PATH to a minimal git-like environment — NO conda/venv bin dir.
    minimal_env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
    }
    # Resolve bash before stripping PATH; the stripped PATH is the condition
    # being tested inside the hook process.
    result = subprocess.run(
        [bash, str(hook)],
        cwd=repo,
        env=minimal_env,
        capture_output=True,
        text=True,
        timeout=10,
        stdin=subprocess.DEVNULL,
    )
    log_path = fake_pjm.parent.parent / "pjm.log"
    if result.returncode != 0:
        reason = _bash_failure_reason(result)
        lowered = reason.lower()
        if "wsl" in lowered or "bash/" in lowered:
            pytest.skip(f"bash is not launchable for hook scripts: {reason[:120]}")
        assert result.returncode == 0, f"hook errored: {reason}"
    assert log_path.exists(), (
        "hook did not invoke pjm — this is the L-047 regression "
        f"(stripped PATH = {minimal_env['PATH']!r}). stderr: {result.stderr!r}"
    )
    invocation = log_path.read_text().strip()
    assert invocation.startswith("precheck"), f"unexpected args: {invocation!r}"


def test_install_hooks_uses_resolved_path(tmp_path: Path) -> None:
    """Smoke: install_hooks writes a hook whose PJM_BIN is the resolved binary."""
    hooks_dir = tmp_path / ".git" / "hooks"
    hooks_dir.mkdir(parents=True)
    install_hooks(hooks_dir)
    precommit = hooks_dir / "pre-commit"
    assert precommit.exists()
    content = precommit.read_text()
    # The hook must contain a PJM_BIN line with a path; we don't pin which
    # specific path, just that it isn't the bare "pjm" fallback when a real
    # binary was discoverable.
    match = re.search(r'PJM_BIN="([^"]+)"', content)
    assert match, "hook missing PJM_BIN line"
    baked = match.group(1)
    found = shutil.which("pjm") or shutil.which("projectmem")
    if found:
        assert baked == found, f"baked path {baked!r} != resolved {found!r}"
