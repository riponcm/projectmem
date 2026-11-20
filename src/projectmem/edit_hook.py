from __future__ import annotations

import json
import os
import posixpath
import re
import stat
import sys
import time
from pathlib import Path

LOG_ENV = "PJM_EDIT_HOOK_LOG"
EVENTS_PATH = Path(".projectmem") / "events.jsonl"
WORKTREE_SEGMENT = re.compile(r"/\.claude/worktrees/[^/]+/")
LINE_SUFFIX = re.compile(r":\d+(?::\d+)?$")
PATCH_FILE_HEADER = re.compile(
    r"^\*\*\* (?:Update File|Add File|Delete File|Move to): (.+)$", re.MULTILINE
)
SAFE_ID = re.compile(r"[0-9A-Za-z_.-]{1,24}")
SKIPPED_PREFIXES = (".projectmem/", ".claude/")
RESET_EVENTS = {"SessionStart", "PostCompact"}
LESSON_MARKS = {"failed": "x", "partial": "~"}
SHOWN_PER_KIND = 3
TEXT_CHARS = 160

Event = dict[str, object]


def text_of(record: dict[str, object], key: str) -> str:
    value = record.get(key)
    return value if isinstance(value, str) else ""


def safe_id(event: Event) -> str:
    issue_id = text_of(event, "issue_id")
    return issue_id if SAFE_ID.fullmatch(issue_id) else ""


def clip(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= TEXT_CHARS else flat[: TEXT_CHARS - 3].rstrip() + "..."


def mention_pattern(relative: str) -> re.Pattern[str]:
    return re.compile(r"(?<![\w./\\-])" + re.escape(relative) + r"(?![\w/\\-]|\.\w)")


class ProjectMemory:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.lexical_prefix = root.as_posix() + "/"
        events = self.read_events(root / EVENTS_PATH)
        closed = {text_of(e, "issue_id") for e in events if e.get("type") == "fix"}
        retired = {text_of(e, "supersedes") for e in events if e.get("supersedes")}
        issue_files = {
            text_of(e, "issue_id"): self.files_of(e) for e in events if e.get("type") == "issue"
        }
        self.candidates: list[tuple[str, Event, set[str], str]] = []
        for event in events:
            kind = lesson_kind(event, closed, retired)
            if kind:
                files = self.files_of(event) or issue_files.get(text_of(event, "issue_id"), set())
                mention_text = "" if kind == "decision" and files else text_of(event, "summary")
                self.candidates.append((kind, event, files, mention_text))

    @staticmethod
    def read_events(events_file: Path) -> list[Event]:
        events: list[Event] = []
        for line in events_file.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                events.append(parsed)
        return events

    def normalized(self, path: str) -> str:
        cleaned = LINE_SUFFIX.sub("", path.strip()).replace("\\", "/")
        if posixpath.isabs(cleaned):
            if cleaned.startswith(self.lexical_prefix):
                return cleaned.removeprefix(self.lexical_prefix)
            try:
                return Path(cleaned).resolve().relative_to(self.root).as_posix()
            except (OSError, ValueError):
                return cleaned
        return posixpath.normpath(cleaned)

    def files_of(self, event: Event) -> set[str]:
        listed = event.get("files")
        paths = [*(listed if isinstance(listed, list) else []), text_of(event, "location")]
        return {self.normalized(path) for path in paths if isinstance(path, str) and path.strip()}

    def lessons(self, relative: str) -> dict[str, list[Event]]:
        mentioned = mention_pattern(relative)
        found: dict[str, list[Event]] = {"issue": [], "attempt": [], "decision": []}
        for kind, event, files, mention_text in self.candidates:
            if relative in files or (relative in mention_text and mentioned.search(mention_text)):
                found[kind].append(event)
        return found

    def report(self, relative: str) -> str:
        found = self.lessons(relative)
        open_issues, attempts, decisions = found["issue"], found["attempt"], found["decision"]
        if not (open_issues or attempts or decisions):
            return ""
        lines = [f"projectmem lessons for {relative}:"]
        if open_issues:
            lines.append("  Open issues on this file:")
            lines.extend(
                f"    - {issue_label(e)}{clip(text_of(e, 'summary'))}"
                for e in open_issues[-SHOWN_PER_KIND:]
            )
            lines.extend(overflow_line(open_issues))
        if attempts:
            failed = sum(text_of(e, "outcome") == "failed" for e in attempts)
            lines.append(
                f"  Earlier attempts ({failed} failed, {len(attempts) - failed} partial), newest first:"
            )
            lines.extend(
                f"    {LESSON_MARKS[text_of(e, 'outcome')]} {clip(text_of(e, 'summary'))}{provenance(e)}"
                for e in reversed(attempts[-SHOWN_PER_KIND:])
            )
            lines.extend(overflow_line(attempts))
        if decisions:
            lines.append("  Decisions on this file:")
            lines.extend(
                f"    - {clip(text_of(e, 'summary'))}{provenance(e, with_issue=False)}"
                for e in decisions[-SHOWN_PER_KIND:]
            )
            lines.extend(overflow_line(decisions))
        return "\n".join(lines)


def lesson_kind(event: Event, closed: set[str], retired: set[str]) -> str:
    kind = event.get("type")
    if kind == "issue" and text_of(event, "issue_id") not in closed:
        return "issue"
    if kind == "attempt" and text_of(event, "outcome") in LESSON_MARKS:
        return "attempt"
    if (
        kind == "decision"
        and text_of(event, "id") not in retired
        and not text_of(event, "capture_source").startswith("git_")
    ):
        return "decision"
    return ""


def issue_label(event: Event) -> str:
    issue_id = safe_id(event)
    return f"#{issue_id} " if issue_id else ""


def provenance(event: Event, with_issue: bool = True) -> str:
    issue_id = safe_id(event) if with_issue else ""
    date = text_of(event, "timestamp")[:10]
    parts = [f"#{issue_id}" if issue_id else "", date]
    shown = ", ".join(part for part in parts if part)
    return f" ({shown})" if shown else ""


def overflow_line(entries: list[Event]) -> list[str]:
    hidden = entries[:-SHOWN_PER_KIND]
    if not hidden:
        return []
    ids = [f"#{issue_id}" for issue_id in dict.fromkeys(safe_id(e) for e in reversed(hidden)) if issue_id]
    source = f", from {', '.join(ids)} (get_issue)" if ids else ""
    return [f"    ... and {len(hidden)} more{source}"]


def edited_paths(tool_input: object) -> list[str]:
    if not isinstance(tool_input, dict):
        return []
    file_path = tool_input.get("file_path")
    if isinstance(file_path, str):
        return [file_path]
    command = tool_input.get("command")
    if isinstance(command, list):
        command = "\n".join(part for part in command if isinstance(part, str))
    return [path.strip() for path in PATCH_FILE_HEADER.findall(command)] if isinstance(command, str) else []


def project_root(path: Path) -> Path | None:
    return next((parent for parent in path.parents if (parent / EVENTS_PATH).is_file()), None)


def locate(path: Path) -> tuple[Path, Path] | None:
    main_checkout = Path(WORKTREE_SEGMENT.sub("/", path.as_posix(), count=1))
    for candidate in dict.fromkeys((main_checkout, path)):
        root = project_root(candidate)
        if root is not None:
            return root, candidate
    return None


def files_by_project(paths: list[str], cwd: str) -> dict[Path, list[str]]:
    grouped: dict[Path, dict[str, None]] = {}
    for raw in paths:
        located = locate(Path(cwd, raw).resolve())
        if located is None:
            continue
        root, path = located
        relative = path.relative_to(root).as_posix()
        if not relative.startswith(SKIPPED_PREFIXES):
            grouped.setdefault(root, {})[relative] = None
    return {root: list(relatives) for root, relatives in grouped.items()}


def seen_root() -> Path | None:
    import tempfile

    owner = str(os.getuid()) if hasattr(os, "getuid") else "user"
    base = Path(tempfile.gettempdir()) / f"pjm-edit-hook-{owner}"
    try:
        base.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = base.lstat()
    if not stat.S_ISDIR(info.st_mode):
        return None
    if hasattr(os, "getuid") and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        return None
    return base


def session_key(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", session_id)[:128]


def report_is_new(session_id: str, agent_id: str, file_key: str, report: str) -> bool:
    if not session_id:
        return True
    try:
        base = seen_root()
        if base is None:
            return True
        marker = base / session_key(session_id) / (session_key(agent_id) or "main") / marker_name(file_key)
        if marker.is_file() and marker.read_text(encoding="utf-8") == report:
            return False
        marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        pending = marker.with_name(f"{marker.name}.{os.getpid()}")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(pending, flags, 0o600), "w", encoding="utf-8") as out:
            out.write(report)
        os.replace(pending, marker)
    except OSError:
        return True
    return True


def forget_session(session_id: str) -> None:
    if not session_id:
        return
    try:
        base = seen_root()
    except OSError:
        return
    if base is not None:
        import shutil

        shutil.rmtree(base / session_key(session_id), ignore_errors=True)


def marker_name(file_key: str) -> str:
    import zlib

    return f"{zlib.crc32(file_key.encode('utf-8')):08x}"


def hook_payload(raw: bytes) -> dict[str, object]:
    try:
        payload = json.loads(raw)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def lessons_for(payload: dict[str, object]) -> str:
    cwd = text_of(payload, "cwd") or os.getcwd()
    session_id = text_of(payload, "session_id")
    agent_id = text_of(payload, "agent_id")
    reports: list[str] = []
    for root, relatives in files_by_project(edited_paths(payload.get("tool_input")), cwd).items():
        memory = ProjectMemory(root)
        reports.extend(
            report
            for relative in relatives
            if (report := memory.report(relative))
            and report_is_new(session_id, agent_id, f"{root.as_posix()}/{relative}", report)
        )
    return "\n\n".join(reports)


def run(payload: dict[str, object]) -> str:
    if text_of(payload, "hook_event_name") in RESET_EVENTS:
        forget_session(text_of(payload, "session_id"))
        return ""
    context = lessons_for(payload)
    if not context:
        return ""
    return json.dumps({
        "hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": context}
    })


def call_record(payload: dict[str, object], output: str, elapsed_ms: float, failure: str) -> dict[str, object]:
    return {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "event": text_of(payload, "hook_event_name"),
        "tool": text_of(payload, "tool_name"),
        "session": text_of(payload, "session_id"),
        "agent": text_of(payload, "agent_id"),
        "cwd": text_of(payload, "cwd"),
        "files": len(edited_paths(payload.get("tool_input"))),
        "reported": output.count("projectmem lessons for "),
        "chars": len(output),
        "ms": round(elapsed_ms, 1),
        "error": failure,
    }


def append_log(log_path: str, record: dict[str, object]) -> None:
    try:
        with open(log_path, "a", encoding="utf-8") as out:
            out.write(json.dumps(record) + "\n")
    except (OSError, ValueError):
        return


def main() -> None:
    started = time.perf_counter()
    payload: dict[str, object] = {}
    output = ""
    failure = ""
    try:
        payload = hook_payload(sys.stdin.buffer.read())
        output = run(payload)
    except Exception as error:  # noqa: BLE001
        failure = f"{type(error).__name__}: {error}"[:300]
    if output:
        sys.stdout.write(output + "\n")
    log_path = os.environ.get(LOG_ENV, "")
    if log_path:
        append_log(log_path, call_record(payload, output, (time.perf_counter() - started) * 1000, failure))


if __name__ == "__main__":
    main()
