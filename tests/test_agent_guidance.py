"""What we actually tell the model.

Two reports against 0.3.2 that were not code bugs but gaps in guidance:

  #14 — Antigravity called tools without a project name, got refused, read the
        error, and retried correctly. A wasted round trip every session,
        because nothing ever told it the name.

  #17 — summary.md accumulated contradicting decisions. `supersedes` has
        existed since 0.1.4 and the summary renderer honours it, but
        AI_INSTRUCTIONS.md mentioned it zero times in 12,503 characters, so
        models never called it. The reporter's own model said as much.

These assert the guidance exists on every surface a client might read. They are
deliberately about content, not formatting.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from projectmem.commands.init import _claude_md_bridge
from projectmem.storage import RECORD_CHARS, TITLE_CHARS, ai_instructions


def _mcp_instructions() -> str:
    from projectmem import mcp_server

    return mcp_server.mcp.instructions or ""


# ── #14: the agent should not have to learn its project name from an error ──

def test_the_bridge_names_the_project():
    bridge = _claude_md_bridge("checkout-api")
    assert "checkout-api" in bridge
    assert 'project="checkout-api"' in bridge


def test_the_bridge_is_still_valid_without_a_name():
    """Registration is best-effort; a missing name must not break the block."""
    bridge = _claude_md_bridge(None)
    assert "projectmem (MANDATORY)" in bridge
    assert "registered with projectmem as" not in bridge


def test_init_writes_the_registered_name_into_claude_md(tmp_path, monkeypatch):
    from conftest import set_fake_home
    from projectmem.commands.init import run as init_run

    set_fake_home(monkeypatch, tmp_path / "home")
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "pm"))
    project = tmp_path / "checkout-api"
    project.mkdir()
    monkeypatch.chdir(project)
    init_run(root=project)

    text = (project / "CLAUDE.md").read_text(encoding="utf-8")
    assert "checkout-api" in text, "an agent reading CLAUDE.md cannot learn the name"


# ── #17: retiring a decision has to be discoverable ─────────────────────────

@pytest.mark.parametrize("surface,getter", [
    ("AI_INSTRUCTIONS.md (get_instructions)", ai_instructions),
    ("CLAUDE.md bridge", lambda: _claude_md_bridge("demo")),
    ("MCP instructions= field", _mcp_instructions),
])
def test_supersedes_is_documented_on_every_surface(surface, getter):
    """A mechanism a model cannot discover is a mechanism that does not exist."""
    text = getter().lower()
    assert "supersede" in text, f"{surface} never mentions supersedes"


# ── #18: every call shape we show must use the tool's real arguments ────────
#
# 0.3.2 told agents to call precheck_file(path); the parameter is file_path, so
# every call failed validation — the guidance was fixed by hand (#22) and the
# README still said get_issue(id) where the parameter is issue_id. Agents copy
# the call shape they are shown, so a wrong name is a failed call every time.
# Rather than pin one tool at a time, read every tool's real signature off the
# server and check every call written on every surface that teaches one.

_REPO = Path(__file__).resolve().parents[1]


def _tool_params() -> dict[str, list[str]]:
    """{tool name: its real parameter names}, from what the server registers."""
    import asyncio
    import inspect

    from projectmem import mcp_server

    params = {}
    for tool in asyncio.run(mcp_server.mcp.list_tools()):
        fn = getattr(mcp_server, tool.name)
        fn = getattr(fn, "fn", fn)  # some SDK versions wrap the callable
        params[tool.name] = list(inspect.signature(fn).parameters)
    return params


def _split_args(arglist: str) -> list[str]:
    """Split a call's argument text on top-level commas, ignoring quoted ones."""
    args, depth, quote, cur = [], 0, None, []
    for ch in arglist:
        if quote:
            quote = None if ch == quote else quote
        elif ch in "\"'":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            args.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    args.append("".join(cur))
    return [a.strip() for a in args if a.strip()]


def _argument_names_shown(text: str, tool: str) -> list[str]:
    """Every argument *name* written in a `tool(...)` call in `text`.

    Quoted values and literals are not names: `outcome="failed"` names
    `outcome`, and `precheck_file('index.html')` names nothing. A bare
    identifier (`precheck_file(file_path)`) is a placeholder for an argument,
    so it has to be one.
    """
    import re

    names = []
    for call in re.finditer(rf"(?<![\w.]){tool}\(([^()]*)\)", text):
        for arg in _split_args(call.group(1)):
            keyword = re.match(r"(\w+)\s*=(?!=)", arg)
            if keyword:
                names.append(keyword.group(1))
            elif re.fullmatch(r"[A-Za-z_]\w*", arg) and arg not in {"None", "True", "False"}:
                names.append(arg)
    return names


def _doc_files() -> list[Path]:
    found = [_REPO / "README.md", _REPO / "TUTORIAL.md", _REPO / "llms.txt"]
    found += sorted((_REPO / "docs").glob("**/*.md"))
    return [f for f in found if f.exists()]


_SURFACES = [
    ("AI_INSTRUCTIONS.md (get_instructions)", ai_instructions),
    ("CLAUDE.md bridge", lambda: _claude_md_bridge("demo")),
    ("MCP instructions= field", _mcp_instructions),
] + [
    (f.name if f.parent == _REPO else f"docs/{f.name}",
     lambda f=f: f.read_text(encoding="utf-8"))
    for f in _doc_files()
]


@pytest.mark.parametrize("surface,getter", _SURFACES, ids=[s for s, _ in _SURFACES])
def test_every_call_shown_uses_the_tools_real_arguments(surface, getter):
    """No surface may show a tool being called with an argument it does not have."""
    text = getter()
    wrong = []
    for tool, params in _tool_params().items():
        for name in _argument_names_shown(text, tool):
            if name not in params:
                wrong.append(f"{tool}({name}) — real arguments: {params or 'none'}")
    assert not wrong, f"{surface} shows calls that fail validation: {wrong}"


def _required_params() -> dict[str, set[str]]:
    """{tool name: the arguments its schema requires}."""
    import asyncio

    from projectmem import mcp_server

    return {
        tool.name: set((getattr(tool, "inputSchema", None) or tool.input_schema).get("required", []))
        for tool in asyncio.run(mcp_server.mcp.list_tools())
    }


@pytest.mark.parametrize("surface,getter", _SURFACES, ids=[s for s, _ in _SURFACES])
def test_every_call_shown_passes_the_tools_required_arguments(surface, getter):
    """The other half: `search_events()` named no wrong argument, so the sweep
    above passed it — but `query` is required and the call fails validation.
    A literal argument (`precheck_file('index.html')`) fills one positionally."""
    import re

    text = getter()
    missing = []
    for tool, required in _required_params().items():
        if not required:
            continue
        for call in re.finditer(rf"(?<![\w.]){tool}\(([^()]*)\)", text):
            args = _split_args(call.group(1))
            named = set(_argument_names_shown(f"{tool}({call.group(1)})", tool))
            literals = len(args) - len(named)
            if len(required - named) > literals:
                missing.append(f"{call.group(0)} — requires {sorted(required)}")
    assert not missing, f"{surface} shows calls missing a required argument: {missing}"


def test_precheck_file_is_shown_on_every_runtime_surface():
    """The sweep above passes vacuously if a surface stops showing the call."""
    for name, getter in _SURFACES[:3]:
        assert _argument_names_shown(getter(), "precheck_file"), (
            f"{name} never shows how to call precheck_file"
        )


def test_the_call_scanner_reads_names_not_values():
    shown = _argument_names_shown(
        'record_attempt(summary, outcome="failed") then record_fix(summary, '
        'issue_id="<issue_id>") and precheck_file(\'index.html\') '
        'and get_issue(id) and search_events(query="a, b=c")',
        "record_attempt",
    )
    assert shown == ["summary", "outcome"]
    assert _argument_names_shown("precheck_file('index.html')", "precheck_file") == []
    assert _argument_names_shown("get_issue(id)", "get_issue") == ["id"]
    assert _argument_names_shown('search_events(query="a, b=c")', "search_events") == ["query"]
    assert _argument_names_shown("get_summary()", "get_summary") == []


def test_supersedes_guidance_says_what_it_does_to_the_summary():
    """Knowing the argument exists is not enough — it must say why to use it."""
    text = ai_instructions().lower()
    assert "append-only" in text
    assert "summary.md" in text
    # the actual call shape, in both flavours
    assert "supersedes=" in text or "--supersedes" in text


def test_instructions_state_the_record_length_the_summary_clips_at():
    text = ai_instructions()
    assert f"issue title under {TITLE_CHARS} characters" in text
    assert f"clips longer records at {RECORD_CHARS} characters" in text


def test_every_write_tool_states_its_record_length():
    import asyncio

    from projectmem import mcp_server

    limits = {
        "log_issue": TITLE_CHARS,
        "record_attempt": RECORD_CHARS,
        "record_fix": RECORD_CHARS,
        "add_decision": RECORD_CHARS,
        "add_note": RECORD_CHARS,
    }
    descriptions = {
        tool.name: tool.input_schema["properties"]["summary"]["description"]
        for tool in asyncio.run(mcp_server.mcp.list_tools())
        if tool.name in limits
    }
    missing = [
        tool for tool, limit in limits.items()
        if f"under {limit} characters" not in descriptions[tool]
    ]
    assert not missing, f"no length limit in the summary description of {missing}"


def test_the_three_surfaces_agree_about_the_session_start_order():
    """There is a standing warning about these drifting apart.

    A past divergence had CLAUDE.md saying "call get_summary first" while the
    MCP field said get_instructions — clients that read both got contradictory
    orders.
    """
    import re

    # AI_INSTRUCTIONS.md is deliberately not in this list: it *is* the content
    # of step 1, so it does not tell you to call step 1 again. Only the two
    # surfaces that advertise the trio can disagree about its order.
    for name, text in (("bridge", _claude_md_bridge("demo")),
                       ("instructions=", _mcp_instructions())):
        # Anchor on the numbered session-start list, not on the first mention
        # anywhere: the prose also cites get_summary when explaining token cost,
        # which says nothing about ordering.
        ordered = re.findall(r"\d\.\s+`?(get_\w+)", text)
        assert ordered[:2] == ["get_instructions", "get_summary"], (
            f"{name} lists the session-start trio as {ordered[:3]}"
        )


# ── a retired decision must not resurface anywhere an agent reads ───────────
#
# Caught in Antigravity: summary.md correctly showed one decision while
# get_context handed the model both — the retired one and its replacement,
# with equal timestamps and nothing to distinguish them. A model could do
# everything right and still be told two contradictory things, which is the
# exact failure superseding exists to prevent.

def _decisions(tmp_path):
    """A retired decision and the one that replaced it.

    Timestamps are relative: generate_context scores on a 30-day recency
    window, so fixed dates silently fall out of scope and the test passes for
    the wrong reason — an empty context contains no retired decision either.
    """
    from datetime import datetime, timedelta, timezone

    from projectmem.models import Event

    now = datetime.now(timezone.utc)
    stamp = lambda days: (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    old = Event(id="evt_old", type="decision", timestamp=stamp(3),
                summary="Use Python http.server for the web UI",
                location="src/api/routes.py:1")
    new = Event(id="evt_new", type="decision", timestamp=stamp(1),
                summary="Switch to TypeScript for the web UI",
                location="src/api/routes.py:1", supersedes="evt_old")
    return [old, new]


def test_get_context_drops_a_superseded_decision(tmp_path):
    from projectmem.commands.context import generate_context
    from projectmem.storage import initialize

    initialize(tmp_path)          # generate_context reads the project's own dir
    md = generate_context(_decisions(tmp_path), token_budget=2000, root=tmp_path)["markdown"]

    assert "TypeScript" in md, "the current decision must survive"
    assert "http.server" not in md, "the retired decision was presented as current"


def test_precheck_does_not_warn_from_a_retired_event(tmp_path):
    """Warning off an approach someone explicitly retired is worse than silence."""
    from projectmem.commands.precheck import _events_for_file

    live = _events_for_file("src/api/routes.py", _decisions(tmp_path))

    assert [e.id for e in live] == ["evt_new"]


def test_every_agent_facing_surface_filters_superseded():
    """Five surfaces filtered; context and precheck did not. Pin all of them."""
    import inspect

    from projectmem import summary as summary_mod
    from projectmem.commands import brief, context, export, precheck, search

    for mod in (summary_mod, brief, context, export, precheck, search):
        assert "superseded_ids" in inspect.getsource(mod), (
            f"{mod.__name__} does not filter retired events"
        )


# ── the bridge must reach clients that do not read CLAUDE.md ────────────────
#
# Antigravity never reads CLAUDE.md — a user reported copying the block into
# AGENTS.md by hand to make projectmem work at all (#14). AGENTS.md is a
# cross-tool convention, not an Antigravity one, so both files are written
# always rather than on detection: pjm init runs once and the choice of client
# comes later.

def test_init_writes_the_bridge_to_both_rule_files(tmp_path, monkeypatch):
    from conftest import set_fake_home
    from projectmem.commands.init import run as init_run

    set_fake_home(monkeypatch, tmp_path / "home")
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "pm"))
    project = tmp_path / "checkout-api"
    project.mkdir()
    monkeypatch.chdir(project)
    init_run(root=project)

    for name in ("CLAUDE.md", "AGENTS.md"):
        text = (project / name).read_text(encoding="utf-8")
        assert "projectmem (MANDATORY)" in text, f"{name} has no bridge"
        assert "checkout-api" in text, f"{name} does not name the project"


def test_both_rule_files_say_exactly_the_same_thing(tmp_path, monkeypatch):
    """Two copies that can drift are worse than one that is missing."""
    from conftest import set_fake_home
    from projectmem.commands.init import (
        _CLAUDE_MD_BRIDGE_END,
        _CLAUDE_MD_BRIDGE_START,
        run as init_run,
    )

    set_fake_home(monkeypatch, tmp_path / "home")
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "pm"))
    project = tmp_path / "app"
    project.mkdir()
    monkeypatch.chdir(project)
    init_run(root=project)

    def block(name):
        t = (project / name).read_text(encoding="utf-8")
        return t[t.index(_CLAUDE_MD_BRIDGE_START):t.index(_CLAUDE_MD_BRIDGE_END)]

    assert block("CLAUDE.md") == block("AGENTS.md")


def test_an_existing_agents_md_is_preserved(tmp_path, monkeypatch):
    """Users already have AGENTS.md files. Do not clobber them."""
    from conftest import set_fake_home
    from projectmem.commands.init import run as init_run

    set_fake_home(monkeypatch, tmp_path / "home")
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "pm"))
    project = tmp_path / "app"
    project.mkdir()
    (project / "AGENTS.md").write_text("# House rules\n\nAlways run the tests.\n")
    monkeypatch.chdir(project)
    init_run(root=project)

    text = (project / "AGENTS.md").read_text(encoding="utf-8")
    assert "Always run the tests." in text, "existing content was destroyed"
    assert "projectmem (MANDATORY)" in text


def test_re_running_init_does_not_duplicate_the_block(tmp_path, monkeypatch):
    from conftest import set_fake_home
    from projectmem.commands.init import _CLAUDE_MD_BRIDGE_START, run as init_run

    set_fake_home(monkeypatch, tmp_path / "home")
    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "pm"))
    project = tmp_path / "app"
    project.mkdir()
    monkeypatch.chdir(project)
    init_run(root=project)
    init_run(root=project)

    for name in ("CLAUDE.md", "AGENTS.md"):
        text = (project / name).read_text(encoding="utf-8")
        assert text.count(_CLAUDE_MD_BRIDGE_START) == 1, f"{name} has a duplicate block"


def test_list_projects_leads_with_the_active_project(tmp_path, monkeypatch):
    """The one name the caller wants should not be on line 14 of 16."""
    from projectmem import mcp_server
    from projectmem.project_registry import register, set_active
    from projectmem.storage import initialize

    monkeypatch.setenv("PROJECTMEM_HOME", str(tmp_path / "pm"))
    for name in ("alpha", "beta", "gamma"):
        d = tmp_path / name
        d.mkdir()
        initialize(d)
        register(d)
    set_active("beta")

    fn = mcp_server.list_projects
    out = (fn.fn if hasattr(fn, "fn") else fn)()

    assert out.splitlines()[0].startswith("ACTIVE: beta")
    assert "You do not need to pass it" in out


def test_instructions_do_not_tell_the_agent_to_list_projects_first():
    """We were telling it to, then counting the call as waste."""
    from projectmem.mcp_server import _GLOBAL_INSTRUCTIONS

    text = _GLOBAL_INSTRUCTIONS.lower()
    assert "do not look the project up before you start" in text
    assert "only if a call has actually failed" in text
