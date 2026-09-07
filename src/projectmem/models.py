from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


VALID_EVENT_TYPES = {
    "issue",
    "hypothesis",
    "attempt",
    "fix",
    "decision",
    "note",
}

VALID_OUTCOMES = {"worked", "failed", "partial"}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def normalize_timestamp(ts: str | None) -> str:
    """Canonicalize any timestamp string to ISO-8601 Zulu (YYYY-MM-DDTHH:MM:SSZ).

    Accepts:
      - ISO-8601 with Z or +00:00 (already canonical — returned as-is after parse)
      - Git's `%ai` format: "2026-05-12 21:07:46 -0600"
      - Anything else `datetime.fromisoformat` accepts

    Returns the input unchanged if it cannot be parsed (so events without
    proper timestamps still round-trip, but writes from new code paths are
    always canonical).
    """
    if not ts:
        return utc_now_iso()
    try:
        # ISO-8601 (with Z suffix)
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        try:
            # Git's %ai format: "YYYY-MM-DD HH:MM:SS ±HHMM"
            dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S %z")
        except ValueError:
            return ts  # let downstream handle / fall back to "Invalid Date"
    return (
        dt.astimezone(timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )



# Extensions that make a dotted token a file rather than an identifier.
# "auth.py" is a file; "AuthService.validate" is a method, and counting it as a
# file would put a phantom entry in the score's file coverage.
_SOURCE_SUFFIXES = {
    "py", "pyi", "js", "jsx", "ts", "tsx", "mjs", "cjs", "rs", "go", "rb", "php",
    "java", "kt", "kts", "swift", "c", "h", "cc", "cpp", "hpp", "cs", "m", "mm",
    "sh", "bash", "zsh", "ps1", "sql", "html", "css", "scss", "sass", "vue",
    "svelte", "json", "yaml", "yml", "toml", "ini", "cfg", "conf", "xml", "md",
    "rst", "txt", "lock", "dockerfile", "gradle", "tf", "proto", "graphql", "ex",
    "exs", "erl", "hs", "scala", "clj", "lua", "r", "jl", "dart", "zig", "nim",
}

_LINE_SUFFIX = re.compile(r":\d+(?::\d+)?$")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


def location_to_file(location: str | None) -> str | None:
    """The file part of an event location, or None if it does not name a file.

    Locations are written as ``file``, ``file:line``, ``file:line:col``, or a
    non-file identifier such as ``ClassName.method``.

    Callers used to do ``location.split(":")[0]``, which has two faults. A bare
    ``src/foo.py`` has no colon, so score's file coverage counted nothing —
    despite ``--at`` inviting exactly that, and costing up to 20 points in
    silence. And a Windows path carries a drive colon: ``C:\\src\\foo.py:42``
    split on the first colon yields ``"C"``.
    """
    if not location:
        return None
    loc = location.strip().strip("\"'")
    if not loc:
        return None

    # Protect the drive colon before stripping a line suffix.
    drive = ""
    if _WINDOWS_DRIVE.match(loc):
        drive, loc = loc[:2], loc[2:]

    loc = _LINE_SUFFIX.sub("", loc)
    if not loc:
        return None

    candidate = drive + loc
    if "/" in loc or "\\" in loc:
        return candidate

    # No separator: only a recognised source suffix makes this a file.
    if "." in loc:
        suffix = loc.rsplit(".", 1)[1].lower()
        if suffix in _SOURCE_SUFFIXES:
            return candidate
    return None

VALID_CAPTURE_SOURCES = {
    "git_post_commit",
    "git_post_revert",
    "git_post_merge",
    "churn_detector",
    "ci_parser",
}

VALID_CONFIDENCE_LEVELS = {"high", "medium", "low"}


@dataclass
class Event:
    type: str
    summary: str
    id: str = field(default_factory=lambda: f"evt_{uuid4().hex[:20]}")
    timestamp: str = field(default_factory=utc_now_iso)
    issue_id: str | None = None
    outcome: str | None = None
    files: list[str] = field(default_factory=list)
    command: str | None = None
    notes: str | None = None
    git_commit: str | None = None
    location: str | None = None
    # Auto-capture fields (P0)
    auto_captured: bool = False
    capture_source: str | None = None
    capture_confidence: str | None = None
    git_message: str | None = None
    # Supersede pointer (0.1.4): this event retires the referenced event id.
    # The back-reference is computed at read time — events.jsonl stays
    # append-only and history is never mutated.
    supersedes: str | None = None

    def __post_init__(self) -> None:
        if self.type not in VALID_EVENT_TYPES:
            raise ValueError(f"Unsupported event type: {self.type}")
        if self.outcome is not None and self.outcome not in VALID_OUTCOMES:
            raise ValueError(f"Unsupported outcome: {self.outcome}")
        if self.capture_source is not None and self.capture_source not in VALID_CAPTURE_SOURCES:
            raise ValueError(f"Unsupported capture source: {self.capture_source}")
        if self.capture_confidence is not None and self.capture_confidence not in VALID_CONFIDENCE_LEVELS:
            raise ValueError(f"Unsupported confidence level: {self.capture_confidence}")
        self.summary = self.summary.strip()
        if not self.summary:
            raise ValueError("Event summary cannot be empty")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return {key: value for key, value in data.items() if value not in (None, [], False)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Event":
        return cls(
            id=data.get("id") or f"evt_{uuid4().hex[:20]}",
            timestamp=normalize_timestamp(data.get("timestamp")) if data.get("timestamp") else utc_now_iso(),
            type=data["type"],
            issue_id=data.get("issue_id"),
            summary=data["summary"],
            outcome=data.get("outcome"),
            files=list(data.get("files") or []),
            command=data.get("command"),
            notes=data.get("notes"),
            git_commit=data.get("git_commit"),
            location=data.get("location"),
            auto_captured=bool(data.get("auto_captured", False)),
            capture_source=data.get("capture_source"),
            capture_confidence=data.get("capture_confidence"),
            git_message=data.get("git_message"),
            supersedes=data.get("supersedes"),
        )


def superseded_ids(events: list["Event"]) -> set[str]:
    """IDs of events retired by a later event's `supersedes` pointer.

    Computed at read time so the log stays append-only — no event line is
    ever rewritten when something supersedes it.
    """
    return {e.supersedes for e in events if e.supersedes}


def resolve_event_ref(events: list["Event"], ref: str) -> "Event":
    """Resolve a user-supplied event reference to a single event.

    Accepts a full event id (``evt_ab12...``) or any unique prefix of the
    hex part (``ab12``). Raises ValueError when nothing matches or the
    prefix is ambiguous — the caller surfaces that message to the user.
    """
    needle = ref.strip()
    if not needle:
        raise ValueError("Empty event reference")
    candidates = [e for e in events if e.id == needle]
    if not candidates:
        bare = needle.removeprefix("evt_")
        candidates = [e for e in events if e.id.removeprefix("evt_").startswith(bare)]
    if not candidates:
        raise ValueError(
            f"No event matches '{ref}'. Use `pjm search <text>` to find the "
            f"event id (shown as evt_...)."
        )
    if len(candidates) > 1:
        raise ValueError(
            f"Event reference '{ref}' is ambiguous ({len(candidates)} matches) — "
            f"use more characters of the id."
        )
    return candidates[0]
