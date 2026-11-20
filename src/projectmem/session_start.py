from __future__ import annotations

from pathlib import Path

from projectmem.storage import (
    ai_instructions_path,
    plan_path,
    project_map_path,
    summary_path,
)
from projectmem.summary import looks_like_placeholder

SETTLED_PLAN_SECTIONS = ("Ideas", "Someday / maybe", "Shipped")
SESSION_START_DONE = (
    "start_session() has already returned what get_instructions, get_summary "
    "and get_plan would at session start: the rules, the memory summary and "
    "the current plan follow. Where the rules below ask for those calls at "
    "session start, they are done."
)
MAP_POINTER = (
    "Project map: call get_project_map() only when the file layout matters."
)


def _text_of(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def _heading_of(section: str) -> str:
    lines = section.splitlines()
    return lines[0].strip() if lines else ""


def current_plan(plan: str) -> str:
    sections = f"\n{plan}".split("\n## ")[1:]
    kept = [
        f"## {section.strip()}"
        for section in sections
        if _heading_of(section) and _heading_of(section) not in SETTLED_PLAN_SECTIONS
    ]
    return "\n\n".join(kept)


def placeholder_map(project_map: str) -> str:
    return project_map if project_map and looks_like_placeholder(project_map) else ""


def build_session_start(root: Path) -> str:
    parts = [
        SESSION_START_DONE,
        _text_of(ai_instructions_path(root)),
        _text_of(summary_path(root)),
        current_plan(_text_of(plan_path(root))),
        placeholder_map(_text_of(project_map_path(root))),
        MAP_POINTER,
    ]
    return "\n\n".join(part for part in parts if part)
