"""`pjm import`. Record shapes follow real exports: agentmemory 0.9.27's
GET /agentmemory/export and mem0 2.2.1's Memory.get_all()."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from projectmem.cli import app
from projectmem.commands import import_memory
from projectmem.models import Event
from projectmem.storage import initialize, read_events

_PRE_KEY = "sk-" + "proj"  # built at run time, like the redaction tests


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "home" / ".pjm"))
    root = tmp_path / "repo"
    root.mkdir()
    initialize(root)
    monkeypatch.chdir(root)
    return root


def _memory(id_: str, type_: str, content: str, project: str, **extra) -> dict:
    return {
        "id": id_, "type": type_, "title": content, "content": content,
        "project": project, "files": [], "isLatest": True, "strength": 7,
        "createdAt": "2026-10-01T09:30:00.000Z", **extra,
    }


def agentmemory_export(project: str, other: str = "/elsewhere/other-project") -> dict:
    return {
        "version": "0.9.27",
        "exportedAt": "2026-11-28T04:04:32.375Z",
        "sessions": [
            {"id": "ses_a", "project": project, "cwd": project, "status": "completed"},
            {"id": "ses_b", "project": other, "cwd": other, "status": "completed"},
        ],
        "memories": [
            _memory("mem_1", "architecture", "Chose Redis for the rate limiter in production.",
                    project, files=[f"{project}/src/ratelimit.ts"]),
            _memory("mem_2", "bug", "JWT check fails silently past 30s of clock skew.",
                    project, files=["src/auth.ts"]),
            _memory("mem_3", "workflow", "Run migrations before the seed script.", project),
            _memory("mem_4", "fact", "Old fact that was replaced.", project, isLatest=False),
            _memory("mem_5", "fact", "Belongs to another project.", other),
            _memory("mem_6", "fact", "No project at all.", ""),
        ],
        "lessons": [
            {"id": "lsn_1", "content": "A mutex around token refresh did not fix the race.",
             "context": "auth", "project": project, "confidence": 0.8,
             "createdAt": "2026-10-02T10:00:00.000Z"},
        ],
        "crystals": [
            {"id": "crys_1", "narrative": "Moved sessions to signed cookies.",
             "keyOutcomes": [], "filesAffected": ["src/session.ts"],
             "lessons": ["A mutex around token refresh did not fix the race."],
             "project": project, "sessionId": "ses_a", "createdAt": "2026-10-03T08:00:00.000Z"},
        ],
        "observations": {
            "ses_a": [
                {"id": "obs_1", "sessionId": "ses_a", "type": "file_write", "title": "Write",
                 "narrative": '{"file_path":"src/x.ts"} | Wrote x', "files": ["src/x.ts"],
                 "timestamp": "2026-10-04T08:00:00.000Z"},
                {"id": "obs_2", "sessionId": "ses_a", "type": "error", "title": "Bash",
                 "narrative": '{"command":"npm test"} | Caching the token in memory broke SSR',
                 "files": ["src/auth.ts"], "timestamp": "2026-10-05T08:00:00.000Z"},
                {"id": "obs_3", "sessionId": "ses_a", "type": "decision", "title": "Decision",
                 "narrative": "Keep the REST API, no GraphQL for now.", "files": [],
                 "timestamp": "2026-10-06T08:00:00.000Z"},
            ],
            "ses_b": [
                {"id": "obs_4", "sessionId": "ses_b", "type": "error", "title": "Bash",
                 "narrative": "Other project's failure", "files": [],
                 "timestamp": "2026-10-07T08:00:00.000Z"},
            ],
        },
        "accessLogs": [{"memoryId": "mem_1"}],
    }


def mem0_export() -> dict:
    return {"results": [
        {"id": "6a35b00d", "memory": "Pydantic v2 broke the settings loader; pinned to 1.10.",
         "hash": "x", "metadata": {"repo": "shop-api"},
         "created_at": "2026-11-28T04:06:30.226354+00:00", "user_id": "u", "agent_id": "a"},
        {"id": "9de2f31a", "memory": "The user prefers short answers.", "hash": "y",
         "metadata": None, "created_at": "2026-11-28T04:06:30.228510+00:00", "user_id": "u"},
    ]}


def _write_json(path: Path, data: object) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _import(*args: str):
    return CliRunner().invoke(app, ["import", *args])


# ── agentmemory ──────────────────────────────────────────────────────────


def test_dry_run_shows_the_plan_and_writes_nothing(repo: Path, tmp_path: Path) -> None:
    export = _write_json(tmp_path / "am.json", agentmemory_export(str(repo)))

    result = _import(str(export), "--from", "agentmemory")

    assert result.exit_code == 0, result.output
    assert "Would import from agentmemory into 'repo': 2 decisions, 4 notes, 1 failed attempt" in result.output
    assert "Nothing written yet" in result.output
    assert read_events(repo) == []


def test_maps_only_onto_projectmem_types(repo: Path) -> None:
    plan = import_memory.plan_agentmemory(agentmemory_export(str(repo)), repo)

    got = {(c.type, c.source_id, c.outcome) for c in plan.candidates}
    assert got == {
        ("decision", "mem_1", None), ("note", "mem_2", None), ("note", "mem_3", None),
        ("note", "lsn_1", None), ("note", "crys_1", None),
        ("attempt", "obs_2", "failed"), ("decision", "obs_3", None),
    }
    assert plan.skipped["superseded in agentmemory"] == 1
    assert plan.skipped["raw activity (reads, writes, commands)"] == 1
    assert plan.skipped["no project named"] == 1
    assert plan.other_projects == {"/elsewhere/other-project": 2}


def test_hook_payload_prefix_is_dropped_and_paths_made_relative(repo: Path) -> None:
    plan = import_memory.plan_agentmemory(agentmemory_export(str(repo)), repo)
    by_id = {c.source_id: c for c in plan.candidates}

    assert by_id["obs_2"].summary == "Caching the token in memory broke SSR"
    assert by_id["mem_1"].files == ["src/ratelimit.ts"]


def test_a_session_started_in_a_subfolder_belongs_to_the_repo(repo: Path) -> None:
    sub = repo / "packages" / "api"
    sub.mkdir(parents=True)
    plan = import_memory.plan_agentmemory(agentmemory_export(str(sub)), repo)

    assert len(plan.candidates) == 7


def test_source_project_picks_a_moved_repo(repo: Path) -> None:
    data = agentmemory_export("/old/place/repo")

    assert import_memory.plan_agentmemory(data, repo).candidates == []
    moved = import_memory.plan_agentmemory(data, repo, source_project="/old/place/repo")
    assert len(moved.candidates) == 7


def test_apply_writes_events_with_their_dates_and_provenance(repo: Path, tmp_path: Path) -> None:
    export = _write_json(tmp_path / "am.json", agentmemory_export(str(repo)))

    result = _import(str(export), "--from", "agentmemory", "--apply")

    assert result.exit_code == 0, result.output
    events = read_events(repo)
    assert len(events) == 7
    failed = next(e for e in events if e.type == "attempt")
    assert failed.outcome == "failed"
    assert failed.location == "src/auth.ts"
    assert failed.timestamp == "2026-10-05T08:00:00Z"
    assert failed.notes == "Imported from agentmemory: obs_2"
    # Nothing an older projectmem would reject.
    for event in events:
        assert Event.from_dict(event.to_dict()).capture_source is None
        assert not event.auto_captured
    summary = (repo / ".projectmem" / "summary.md").read_text(encoding="utf-8")
    assert "Chose Redis for the rate limiter in production." in summary


def test_importing_twice_adds_nothing(repo: Path, tmp_path: Path) -> None:
    export = _write_json(tmp_path / "am.json", agentmemory_export(str(repo)))
    _import(str(export), "--from", "agentmemory", "--apply")

    result = _import(str(export), "--from", "agentmemory", "--apply")

    assert "7 already imported" in result.output
    assert len(read_events(repo)) == 7


def test_skips_what_the_project_already_knows_and_repeats(repo: Path) -> None:
    data = agentmemory_export(str(repo))
    data["lessons"].append({**data["lessons"][0], "id": "lsn_2"})
    existing = [Event(type="note", summary="Run migrations before the SEED script.")]

    plan = import_memory.plan_agentmemory(data, repo)
    import_memory.drop_known(plan, existing)

    ids = {c.source_id for c in plan.candidates}
    assert "mem_3" not in ids and "lsn_2" not in ids
    assert plan.skipped["already in this project's memory"] == 1
    assert plan.skipped["duplicate in the export"] == 1


def test_imported_text_is_redacted(repo: Path, tmp_path: Path) -> None:
    data = agentmemory_export(str(repo))
    data["memories"][2]["content"] = "Deploy uses " + _PRE_KEY + "-" + "A" * 48 + " from the env."
    export = _write_json(tmp_path / "am.json", data)

    _import(str(export), "--from", "agentmemory", "--apply")

    raw = (repo / ".projectmem" / "events.jsonl").read_text(encoding="utf-8")
    assert _PRE_KEY not in raw
    assert "[REDACTED:openai_key]" in raw


def test_a_bad_record_writes_nothing(repo: Path) -> None:
    plan = import_memory.Plan("agentmemory", [
        import_memory.Candidate("note", "fine", "a", "lesson"),
        import_memory.Candidate("attempt", "bad outcome", "b", "x", outcome="exploded"),
    ])

    with pytest.raises(ValueError):
        import_memory.write(plan, repo)
    assert read_events(repo) == []


def test_imported_failures_reach_precheck(repo: Path, tmp_path: Path) -> None:
    (repo / "src").mkdir()
    (repo / "src" / "auth.ts").write_text("x\n", encoding="utf-8")
    export = _write_json(tmp_path / "am.json", agentmemory_export(str(repo)))
    _import(str(export), "--from", "agentmemory", "--apply")

    result = CliRunner().invoke(app, ["precheck", "src/auth.ts"])

    assert "Caching the token in memory broke SSR" in result.output


def test_nothing_from_this_project_suggests_source_project(repo: Path, tmp_path: Path) -> None:
    export = _write_json(tmp_path / "am.json", agentmemory_export("/old/place/repo"))

    result = _import(str(export), "--from", "agentmemory")

    assert "--source-project" in result.output


# ── mem0 ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("shape", ["results", "list", "memories"])
def test_mem0_memories_become_notes(repo: Path, tmp_path: Path, shape: str) -> None:
    items = mem0_export()["results"]
    data = {"results": items} if shape == "results" else items if shape == "list" else {"memories": items}
    export = _write_json(tmp_path / "mem0.json", data)

    result = _import(str(export), "--from", "mem0", "--apply")

    assert result.exit_code == 0, result.output
    events = read_events(repo)
    assert [e.type for e in events] == ["note", "note"]
    assert events[0].notes == "Imported from mem0: 6a35b00d"
    assert events[0].timestamp == "2026-11-28T04:06:30Z"


def test_mem0_has_no_projects(repo: Path, tmp_path: Path) -> None:
    export = _write_json(tmp_path / "mem0.json", mem0_export())

    result = _import(str(export), "--from", "mem0", "--source-project", "/x")

    assert result.exit_code == 1


# ── errors ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("source, data", [
    ("agentmemory", {"results": []}),
    ("mem0", {"sessions": [], "memories": [{"content": "x"}]}),
    ("mem0", "not json"),
])
def test_refuses_a_file_that_is_not_that_export(repo: Path, tmp_path: Path, source: str, data: object) -> None:
    path = tmp_path / "x.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")

    result = _import(str(path), "--from", source)

    assert result.exit_code == 1
    assert "export" in result.output
    assert read_events(repo) == []


def test_unknown_source_and_missing_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    export = _write_json(tmp_path / "m.json", mem0_export())

    assert _import(str(export), "--from", "letta").exit_code == 1
    assert _import(str(export), "--from", "mem0").exit_code == 1  # no .projectmem here
