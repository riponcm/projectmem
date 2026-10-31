"""An existing project's AI_INSTRUCTIONS.md must not stay stale forever.

initialize() writes the file only when it is missing, so every project made
by an older release kept that release's guidance — including call shapes that
were later corrected (precheck_file(path), #18) — and get_instructions() served
it. `pjm init` now refreshes a file whose version marker is absent or old, and
leaves a current one alone so user edits survive.
"""
from __future__ import annotations

from conftest import set_fake_home

from projectmem.commands.init import run as init_run
from projectmem.storage import (
    AI_INSTRUCTIONS_FILE,
    INSTRUCTIONS_BACKUP_FILE,
    INSTRUCTIONS_VERSION,
    ai_instructions,
    initialize,
    instructions_version,
    refresh_ai_instructions,
)

STALE = (
    "# projectmem AI Instructions\n\n"
    "Call `precheck_file(path)` before editing.\n\n"
    "## Global Memory — Inherited Knowledge\n\n"
    "### Library Gotchas\n\n"
    "- **vite**: dev server caches deps (from other-app)\n\n"
    "## Rules summary\n\n"
    "- old rule\n"
)


def _project(tmp_path, monkeypatch):
    set_fake_home(monkeypatch, tmp_path / "home")
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "pm"))
    project = tmp_path / "app"
    project.mkdir()
    monkeypatch.chdir(project)
    return project


def test_the_template_carries_the_current_marker():
    assert instructions_version(ai_instructions()) == INSTRUCTIONS_VERSION
    assert instructions_version(STALE) == 0


def test_a_fresh_init_writes_the_current_file_without_a_backup(tmp_path, monkeypatch, capsys):
    project = _project(tmp_path, monkeypatch)
    init_run(root=project)

    mem = project / ".projectmem"
    assert instructions_version((mem / AI_INSTRUCTIONS_FILE).read_text(encoding="utf-8")) == INSTRUCTIONS_VERSION
    assert not (mem / INSTRUCTIONS_BACKUP_FILE).exists()
    assert "Refreshed" not in capsys.readouterr().out


def test_init_refreshes_a_stale_file_keeping_a_backup_and_the_global_block(
    tmp_path, monkeypatch, capsys
):
    project = _project(tmp_path, monkeypatch)
    initialize(project)
    mem = project / ".projectmem"
    (mem / AI_INSTRUCTIONS_FILE).write_text(STALE, encoding="utf-8")

    init_run(root=project, no_global=True)

    text = (mem / AI_INSTRUCTIONS_FILE).read_text(encoding="utf-8")
    assert "precheck_file(path)" not in text
    assert "precheck_file(file_path)" in text
    assert instructions_version(text) == INSTRUCTIONS_VERSION
    # the inherited knowledge survives the rewrite, in one piece
    assert text.count("## Global Memory — Inherited Knowledge") == 1
    assert "- **vite**: dev server caches deps (from other-app)" in text
    assert text.index("Global Memory") < text.index("## Rules summary")
    # the old copy is kept for anyone who had edited it
    assert (mem / INSTRUCTIONS_BACKUP_FILE).read_text(encoding="utf-8") == STALE
    assert "Refreshed .projectmem/AI_INSTRUCTIONS.md" in capsys.readouterr().out
    # the backup is a local safety copy, not team knowledge to commit
    assert ".projectmem/AI_INSTRUCTIONS.md.bak" in (project / ".gitignore").read_text()


def test_init_leaves_a_current_file_with_user_edits_untouched(tmp_path, monkeypatch, capsys):
    project = _project(tmp_path, monkeypatch)
    initialize(project)
    path = project / ".projectmem" / AI_INSTRUCTIONS_FILE
    edited = path.read_text(encoding="utf-8") + "\n## House rules\n\nAlways squash.\n"
    path.write_text(edited, encoding="utf-8")

    init_run(root=project, no_global=True)

    assert path.read_text(encoding="utf-8") == edited
    assert not (path.parent / INSTRUCTIONS_BACKUP_FILE).exists()
    assert "Refreshed" not in capsys.readouterr().out


def test_an_older_numbered_marker_is_refreshed_too(tmp_path):
    initialize(tmp_path)
    path = tmp_path / ".projectmem" / AI_INSTRUCTIONS_FILE
    path.write_text(
        "<!-- projectmem-instructions: 0 -->\nold\n", encoding="utf-8"
    )
    assert refresh_ai_instructions(tmp_path) is True
    assert instructions_version(path.read_text(encoding="utf-8")) == INSTRUCTIONS_VERSION


def test_a_refresh_with_no_global_block_adds_none(tmp_path):
    initialize(tmp_path)
    path = tmp_path / ".projectmem" / AI_INSTRUCTIONS_FILE
    path.write_text("old, no marker\n", encoding="utf-8")
    refresh_ai_instructions(tmp_path)
    assert "Global Memory" not in path.read_text(encoding="utf-8")


def test_initialize_alone_never_rewrites_the_file(tmp_path):
    """Only `pjm init` refreshes; any other caller of initialize() must not."""
    initialize(tmp_path)
    path = tmp_path / ".projectmem" / AI_INSTRUCTIONS_FILE
    path.write_text(STALE, encoding="utf-8")
    initialize(tmp_path)
    assert path.read_text(encoding="utf-8") == STALE
    assert not (path.parent / INSTRUCTIONS_BACKUP_FILE).exists()


# The template's fingerprint at each INSTRUCTIONS_VERSION. Editing the
# template without bumping the version means no existing project ever gets
# the edit: refresh only rewrites a file whose marker is older. When this
# fails, bump INSTRUCTIONS_VERSION in storage.py and record the new hash.
_TEMPLATE_FINGERPRINTS = {
    1: "e699906caeddbf8a",
    2: "78650ecb093907ef",
}


def test_a_template_change_comes_with_a_version_bump():
    import hashlib

    from projectmem.storage import INSTRUCTIONS_VERSION, ai_instructions

    digest = hashlib.sha256(ai_instructions().encode()).hexdigest()[:16]
    assert _TEMPLATE_FINGERPRINTS.get(INSTRUCTIONS_VERSION) == digest, (
        "ai_instructions() changed: bump INSTRUCTIONS_VERSION and add "
        f"{INSTRUCTIONS_VERSION + 1}: \"<new hash>\" to _TEMPLATE_FINGERPRINTS (now {digest})"
    )
