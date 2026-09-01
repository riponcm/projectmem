"""Cross-Project Global Memory — knowledge that follows the developer.

Stores patterns, library gotchas, and stack preferences in ~/.projectmem/global/.
Automatically inherited by new projects based on detected stack.

Global memory is 100% local — nothing leaves the machine.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


# ── Paths ──
GLOBAL_DIR = Path.home() / ".projectmem" / "global"
PATTERNS_FILE = "patterns.jsonl"
GOTCHAS_FILE = "library_gotchas.jsonl"
PREFERENCES_FILE = "stack_preferences.md"


def global_dir() -> Path:
    """Return the global memory directory, creating it if needed."""
    GLOBAL_DIR.mkdir(parents=True, exist_ok=True)
    return GLOBAL_DIR


def patterns_path() -> Path:
    return global_dir() / PATTERNS_FILE


def gotchas_path() -> Path:
    return global_dir() / GOTCHAS_FILE


def preferences_path() -> Path:
    return global_dir() / PREFERENCES_FILE


# ══════════════════════════════════════════
# Stack Detection
# ══════════════════════════════════════════

# Map of manifest files to detected technologies
STACK_DETECTORS: list[dict[str, Any]] = [
    # Python
    {"file": "pyproject.toml", "tags": ["python"], "parser": "_parse_pyproject"},
    {"file": "requirements.txt", "tags": ["python"], "parser": "_parse_requirements"},
    {"file": "setup.py", "tags": ["python"], "parser": None},
    {"file": "Pipfile", "tags": ["python", "pipenv"], "parser": None},
    # JavaScript / TypeScript
    {"file": "package.json", "tags": ["javascript"], "parser": "_parse_package_json"},
    {"file": "tsconfig.json", "tags": ["typescript"], "parser": None},
    {"file": "bun.lockb", "tags": ["bun"], "parser": None},
    # Rust
    {"file": "Cargo.toml", "tags": ["rust"], "parser": "_parse_cargo"},
    # Go
    {"file": "go.mod", "tags": ["go"], "parser": "_parse_go_mod"},
    # Ruby
    {"file": "Gemfile", "tags": ["ruby"], "parser": None},
    # Java / Kotlin
    {"file": "pom.xml", "tags": ["java", "maven"], "parser": None},
    {"file": "build.gradle", "tags": ["java", "gradle"], "parser": None},
    {"file": "build.gradle.kts", "tags": ["kotlin", "gradle"], "parser": None},
    # Docker
    {"file": "Dockerfile", "tags": ["docker"], "parser": None},
    {"file": "docker-compose.yml", "tags": ["docker"], "parser": None},
    # CI
    {
        "file": ".github/workflows",
        "tags": ["github-actions"],
        "parser": None,
        "is_dir": True,
    },
]


def detect_stack(root: Path) -> dict[str, Any]:
    """Detect the project's technology stack by scanning manifest files.

    Returns:
        {
            "tags": ["python", "fastapi", "sqlalchemy", ...],
            "libraries": ["fastapi", "sqlalchemy", "pydantic", ...],
            "frameworks": ["fastapi"],
            "manifest_files": ["pyproject.toml", "Dockerfile"],
        }
    """
    tags: set[str] = set()
    libraries: set[str] = set()
    manifest_files: list[str] = []

    for detector in STACK_DETECTORS:
        target = root / detector["file"]
        is_dir = detector.get("is_dir", False)

        if (is_dir and target.is_dir()) or (not is_dir and target.exists()):
            tags.update(detector["tags"])
            manifest_files.append(detector["file"])

            # Run parser if available
            parser_name = detector.get("parser")
            if parser_name and not is_dir:
                parser_fn = globals().get(parser_name)
                if parser_fn:
                    result = parser_fn(target)
                    tags.update(result.get("tags", []))
                    libraries.update(result.get("libraries", []))

    # Detect frameworks from libraries. Match against the *whole* library
    # token (split on common separators) so e.g. `eslint-plugin-react` doesn't
    # match `gin` via the substring `gin` ⊂ `plugin` (L-026a false positive).
    frameworks: set[str] = set()
    framework_map = {
        "fastapi": "fastapi",
        "flask": "flask",
        "django": "django",
        "react": "react",
        "vue": "vue",
        "next": "nextjs",
        "express": "express",
        "actix-web": "actix",
        "axum": "axum",
        "gin": "gin",
    }
    for lib in libraries:
        lib_lower = lib.lower()
        tokens_in_lib = set(re.split(r"[-_/@.]", lib_lower))
        tokens_in_lib.add(lib_lower)
        for key, fw in framework_map.items():
            if key in tokens_in_lib:
                frameworks.add(fw)
                tags.add(fw)

    result = {
        "tags": sorted(tags),
        "libraries": sorted(libraries),
        "frameworks": sorted(frameworks),
        "manifest_files": manifest_files,
    }

    # Self-curating promotable set (L-045): every library this project declares
    # becomes eligible for cross-project promotion machine-wide. Best-effort —
    # cache failures must not break stack detection.
    try:
        record_promotable_libraries(result["libraries"] + result["tags"])
    except Exception:
        pass

    return result


def _parse_pyproject(path: Path) -> dict[str, Any]:
    """Extract dependencies from pyproject.toml."""
    tags: set[str] = set()
    libraries: set[str] = set()

    try:
        content = path.read_text(encoding="utf-8")

        # Extract dependencies (simple regex — no toml parser needed)
        dep_pattern = re.compile(
            r'^\s*"?([a-zA-Z0-9_-]+)'
            r'(?:\[.*?\])?'
            r'(?:\s*[><=!~]+.*)?"?\s*,?\s*$',
            re.MULTILINE,
        )

        in_deps = False
        for line in content.split("\n"):
            if re.match(r"\[.*dependencies.*\]", line, re.IGNORECASE):
                in_deps = True
                continue
            if in_deps and line.startswith("["):
                in_deps = False
                continue
            if in_deps:
                m = dep_pattern.match(line)
                if m:
                    lib = m.group(1).lower().strip('"').strip("'")
                    if lib and lib not in ("python", "", "dev", "test", "docs", "all"):
                        libraries.add(lib)

        # Also check for common libraries in the whole file
        known_libs = {
            "sqlalchemy": "sqlalchemy",
            "pydantic": "pydantic",
            "fastapi": "fastapi",
            "flask": "flask",
            "django": "django",
            "pytest": "pytest",
            "typer": "typer",
            "click": "click",
            "celery": "celery",
            "redis": "redis",
            "numpy": "numpy",
            "pandas": "pandas",
            "torch": "pytorch",
            "tensorflow": "tensorflow",
            "aiohttp": "aiohttp",
            "httpx": "httpx",
            "alembic": "alembic",
        }
        # Word-boundary match to avoid e.g. `gin` ⊂ `imagineering` (L-026a).
        content_lower = content.lower()
        for key, tag in known_libs.items():
            if re.search(rf"\b{re.escape(key)}\b", content_lower):
                tags.add(tag)
                libraries.add(key)

    except Exception:
        pass

    return {"tags": list(tags), "libraries": list(libraries)}


def _parse_requirements(path: Path) -> dict[str, Any]:
    """Extract dependencies from requirements.txt."""
    libraries: set[str] = set()
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and not line.startswith("-"):
                lib = re.split(r"[><=!~\[;@]", line)[0].strip().lower()
                if lib:
                    libraries.add(lib)
    except Exception:
        pass
    return {"tags": [], "libraries": list(libraries)}


def _parse_package_json(path: Path) -> dict[str, Any]:
    """Extract dependencies from package.json."""
    tags: set[str] = set()
    libraries: set[str] = set()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        for dep_key in ("dependencies", "devDependencies"):
            deps = data.get(dep_key, {})
            for lib in deps:
                libraries.add(lib.lower())
                # Detect frameworks
                if lib == "react":
                    tags.add("react")
                elif lib == "vue":
                    tags.add("vue")
                elif lib == "next":
                    tags.add("nextjs")
                elif lib == "express":
                    tags.add("express")
                elif lib == "vite":
                    tags.add("vite")
                elif lib == "tailwindcss":
                    tags.add("tailwind")
                elif lib == "typescript":
                    tags.add("typescript")
    except Exception:
        pass
    return {"tags": list(tags), "libraries": list(libraries)}


def _parse_cargo(path: Path) -> dict[str, Any]:
    """Extract dependencies from Cargo.toml."""
    libraries: set[str] = set()
    try:
        in_deps = False
        for line in path.read_text(encoding="utf-8").splitlines():
            if "[dependencies]" in line or "[dev-dependencies]" in line:
                in_deps = True
                continue
            if in_deps and line.startswith("["):
                in_deps = False
                continue
            if in_deps and "=" in line:
                lib = line.split("=")[0].strip()
                if lib:
                    libraries.add(lib.lower())
    except Exception:
        pass
    return {"tags": [], "libraries": list(libraries)}


def _parse_go_mod(path: Path) -> dict[str, Any]:
    """Extract dependencies from go.mod."""
    libraries: set[str] = set()
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("module") and not line.startswith("go "):
                if line.startswith("require") or line.startswith(")"):
                    continue
                parts = line.split()
                if parts:
                    lib = parts[0].split("/")[-1]
                    libraries.add(lib.lower())
    except Exception:
        pass
    return {"tags": [], "libraries": list(libraries)}


# ══════════════════════════════════════════
# Pattern & Gotcha CRUD
# ══════════════════════════════════════════


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file into a list of dicts."""
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entries.append(json.loads(line))
    return entries


def _write_jsonl(path: Path, entries: list[dict[str, Any]]) -> None:
    """Write a list of dicts to a JSONL file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, sort_keys=True) + "\n")


def _append_jsonl(path: Path, entry: dict[str, Any]) -> None:
    """Append a single entry to a JSONL file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")


def read_patterns() -> list[dict[str, Any]]:
    return _read_jsonl(patterns_path())


def read_gotchas() -> list[dict[str, Any]]:
    return _read_jsonl(gotchas_path())


def add_pattern(
    pattern: str,
    tags: list[str] | None = None,
    source_project: str | None = None,
    confidence: str = "medium",
) -> dict[str, Any]:
    """Add a cross-project pattern to global memory."""
    entry = {
        "id": f"pat_{uuid4().hex[:12]}",
        "pattern": pattern,
        "tags": tags or [],
        "source_project": source_project,
        "confidence": confidence,
        "times_encountered": 1,
        "created": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    _append_jsonl(patterns_path(), entry)
    return entry


def add_gotcha(
    library: str,
    gotcha: str,
    tags: list[str] | None = None,
    source_project: str | None = None,
    version_range: str | None = None,
) -> dict[str, Any]:
    """Add a library-specific gotcha to global memory."""
    entry = {
        "id": f"got_{uuid4().hex[:12]}",
        "library": library.lower(),
        "gotcha": gotcha,
        "tags": tags or [],
        "source_project": source_project,
        "version_range": version_range,
        "discovered": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    _append_jsonl(gotchas_path(), entry)
    return entry


def remove_entry(entry_id: str) -> bool:
    """Remove a pattern or gotcha by ID."""
    for path in (patterns_path(), gotchas_path()):
        entries = _read_jsonl(path)
        original_len = len(entries)
        entries = [e for e in entries if e.get("id") != entry_id]
        if len(entries) < original_len:
            _write_jsonl(path, entries)
            return True
    return False


def prune_entries(
    older_than_days: int = 365, confidence: str | None = None
) -> int:
    """Remove old or low-confidence entries. Returns count removed."""
    cutoff = datetime.now(timezone.utc).timestamp() - (older_than_days * 86400)
    removed = 0

    for path in (patterns_path(), gotchas_path()):
        entries = _read_jsonl(path)
        original_len = len(entries)
        filtered = []
        for e in entries:
            ts_field = e.get("created") or e.get("discovered") or ""
            try:
                entry_ts = datetime.fromisoformat(
                    ts_field.replace("Z", "+00:00")
                ).timestamp()
            except (ValueError, AttributeError):
                entry_ts = 0

            # Remove if old AND (no confidence filter or matches confidence)
            if entry_ts < cutoff:
                if confidence is None or e.get("confidence") == confidence:
                    removed += 1
                    continue
            filtered.append(e)

        if len(filtered) < original_len:
            _write_jsonl(path, filtered)

    return removed


# ══════════════════════════════════════════
# Inheritance — inject global memory into new projects
# ══════════════════════════════════════════


def get_relevant_entries(
    stack: dict[str, Any],
    filter_tags: list[str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Get global patterns and gotchas relevant to a project's stack.

    Returns:
        {
            "patterns": [...],
            "gotchas": [...],
        }
    """
    project_tags = set(stack.get("tags", []))
    project_libs = set(stack.get("libraries", []))

    if filter_tags:
        project_tags = set(filter_tags)

    # Match patterns by tag overlap
    patterns = read_patterns()
    relevant_patterns = []
    for p in patterns:
        p_tags = set(p.get("tags", []))
        if not p_tags or p_tags & project_tags:
            relevant_patterns.append(p)

    # Match gotchas by library
    gotchas = read_gotchas()
    relevant_gotchas = []
    for g in gotchas:
        lib = g.get("library", "").lower()
        if lib in project_libs or lib in project_tags:
            relevant_gotchas.append(g)

    return {
        "patterns": relevant_patterns,
        "gotchas": relevant_gotchas,
    }


def build_inherited_instructions(
    relevant: dict[str, list[dict[str, Any]]]
) -> str:
    """Build markdown section for AI_INSTRUCTIONS.md from global memory."""
    patterns = relevant.get("patterns", [])
    gotchas = relevant.get("gotchas", [])

    if not patterns and not gotchas:
        return ""

    lines = [
        "## Global Memory — Inherited Knowledge\n",
        "The following patterns and gotchas were inherited from your global memory",
        "(~/.projectmem/global/). They represent lessons learned from other projects.\n",
    ]

    if gotchas:
        lines.append("### Library Gotchas\n")
        for g in gotchas:
            lib = g.get("library", "unknown")
            gotcha = g.get("gotcha", "")
            source = g.get("source_project", "")
            source_str = f" (from {source})" if source else ""
            version = g.get("version_range", "")
            version_str = f" [{version}]" if version else ""
            lines.append(f"- **{lib}**{version_str}: {gotcha}{source_str}")
        lines.append("")

    if patterns:
        lines.append("### Cross-Project Patterns\n")
        for p in patterns:
            pattern = p.get("pattern", "")
            source = p.get("source_project", "")
            source_str = f" (from {source})" if source else ""
            lines.append(f"- {pattern}{source_str}")
        lines.append("")

    return "\n".join(lines)


# ══════════════════════════════════════════
# Export / Import
# ══════════════════════════════════════════


def export_all() -> dict[str, Any]:
    """Export all global memory as a single JSON structure."""
    prefs = ""
    if preferences_path().exists():
        prefs = preferences_path().read_text(encoding="utf-8")

    return {
        "version": "1.0",
        "exported_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "patterns": read_patterns(),
        "gotchas": read_gotchas(),
        "preferences": prefs,
    }


def import_all(data: dict[str, Any], merge: bool = True) -> dict[str, int]:
    """Import global memory from an exported JSON structure.

    If merge=True, adds new entries without duplicating existing ones.
    If merge=False, replaces everything.

    Returns counts of imported entries.
    """
    if not merge:
        _write_jsonl(patterns_path(), data.get("patterns", []))
        _write_jsonl(gotchas_path(), data.get("gotchas", []))
        if data.get("preferences"):
            preferences_path().write_text(data["preferences"], encoding="utf-8")
        return {
            "patterns": len(data.get("patterns", [])),
            "gotchas": len(data.get("gotchas", [])),
        }

    # Merge mode — skip duplicates by ID
    existing_pattern_ids = {p["id"] for p in read_patterns()}
    existing_gotcha_ids = {g["id"] for g in read_gotchas()}

    new_patterns = 0
    for p in data.get("patterns", []):
        if p.get("id") not in existing_pattern_ids:
            _append_jsonl(patterns_path(), p)
            new_patterns += 1

    new_gotchas = 0
    for g in data.get("gotchas", []):
        if g.get("id") not in existing_gotcha_ids:
            _append_jsonl(gotchas_path(), g)
            new_gotchas += 1

    if data.get("preferences") and not preferences_path().exists():
        preferences_path().write_text(data["preferences"], encoding="utf-8")

    return {"patterns": new_patterns, "gotchas": new_gotchas}


# ══════════════════════════════════════════
# Auto-Promote — detect library-specific events and promote to global
# ══════════════════════════════════════════


# Seed list of libraries projectmem knows about out of the box. The actual
# promotable set is this seed UNION every library detect_stack() has ever
# seen in any manifest on this machine (see PROMOTABLE_CACHE_FILE below).
# That makes the system self-curating: a Go user gets `gin`, `chi`, `cobra`
# promotable just by running `pjm init` in projects that declare them, with
# no JS/Python bias hard-coded in.
PROMOTABLE_SEED = {
    "sqlalchemy", "pydantic", "fastapi", "flask", "django",
    "react", "vue", "next", "express", "vite",
    "pandas", "numpy", "torch", "tensorflow",
    "celery", "redis", "docker", "nginx",
    "pytest", "jest", "vitest",
    "alembic", "prisma", "drizzle",
    "tailwind", "webpack", "esbuild",
}
# Backwards-compatible alias — older code still imports this name.
PROMOTABLE_LIBRARIES = PROMOTABLE_SEED

PROMOTABLE_CACHE_FILE = ".promotable.json"


def promotable_cache_path() -> Path:
    return global_dir() / PROMOTABLE_CACHE_FILE


def load_promotable_set() -> set[str]:
    """Return the seed UNION every library detect_stack has cached.

    The cache is a tiny JSON list at ~/.projectmem/global/.promotable.json,
    populated by `record_promotable_libraries` whenever a project's stack
    is detected. Missing or unreadable cache files fall back to the seed
    alone — auto-promote keeps working even on a fresh machine.
    """
    libs = set(PROMOTABLE_SEED)
    path = promotable_cache_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            for lib in data.get("libraries", []):
                if isinstance(lib, str) and lib:
                    libs.add(lib.lower())
        except (json.JSONDecodeError, OSError):
            pass
    return libs


def record_promotable_libraries(libraries: list[str]) -> None:
    """Persist this project's detected libraries into the machine-wide cache.

    Idempotent: only writes when new libraries appear. The merged list is
    what `auto_promote_event` matches against, so any library declared in
    any manifest on this machine becomes eligible for cross-project promotion.
    """
    new = {lib.lower() for lib in libraries if isinstance(lib, str) and lib}
    if not new:
        return
    existing = load_promotable_set()
    if new.issubset(existing):
        return
    merged = sorted(existing | new)
    path = promotable_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"libraries": merged}, sort_keys=True, indent=2),
        encoding="utf-8",
    )


# Signal prefixes that mark a decision / note as a deliberate cross-project
# lesson rather than a project-local setup choice. Case-insensitive, anchored
# to the start of the summary text. Failed/partial attempts are also auto-
# promoted because the outcome already encodes "this didn't work / didn't
# fully work" — that's the strongest possible lesson signal.
GOTCHA_PREFIXES = (
    "gotcha:",
    "lesson:",
    "warning:",
    "caution:",
    "pitfall:",
    "avoid:",
    "don't ",
    "do not ",
    "never ",
    "bug:",
)


def _is_gotcha_signal(event_summary: str, event_type: str, outcome: str | None) -> bool:
    """Return True when this event is a deliberate cross-project lesson.

    Rules:
    - `attempt` with outcome `failed` or `partial`: always a lesson.
    - `attempt` with outcome `worked` or no outcome: not a lesson — that's
      project-local success, no need to broadcast.
    - `decision` / `note`: only a lesson if the summary text opens with one
      of the GOTCHA_PREFIXES (e.g. "gotcha: vite HMR crashes...").

    Without this filter, every framework-choice decision pollutes the global
    store with project-local setup notes (see L-046).
    """
    if event_type == "attempt":
        return outcome in ("failed", "partial")
    if event_type in ("decision", "note"):
        head = event_summary.lstrip().lower()
        return any(head.startswith(prefix) for prefix in GOTCHA_PREFIXES)
    return False


def auto_promote_event(
    event_summary: str,
    event_type: str,
    project_name: str,
    tags: list[str] | None = None,
    project_libraries: list[str] | None = None,
    outcome: str | None = None,
) -> dict[str, Any] | None:
    """Check if a project event should be promoted to global memory.

    Promotes only events that pass BOTH filters:
      1. Signal filter (L-046) — the event must be a deliberate lesson:
         failed/partial attempts, OR decisions/notes prefixed with one of
         GOTCHA_PREFIXES ("gotcha:", "lesson:", "warning:", ...).
      2. Library filter — the summary must whole-word-match a library that
         this project actually declares (per `project_libraries`), drawn from
         the machine-wide promotable set (seed + every library detect_stack
         has cached, see L-045).

    The signal filter keeps the global store free of project-local setup
    notes ("Use FastAPI as the core web framework..."). The library filter
    keeps English collisions from promoting fake gotchas ("the next HMR
    reload" in a non-Next.js project).

    Returns the created entry or None.
    """
    if event_type not in ("attempt", "decision", "note"):
        return None

    # Signal filter — is this actually a cross-project lesson?
    if not _is_gotcha_signal(event_summary, event_type, outcome):
        return None

    summary_lower = event_summary.lower()
    promotable = load_promotable_set()

    # Whole-word match against the dynamic promotable set. Naive substring
    # match catches false positives like "gin" inside "imagineering"
    # (see L-026a).
    matched_libs = [
        lib
        for lib in sorted(promotable)
        if re.search(rf"\b{re.escape(lib)}\b", summary_lower)
    ]
    if not matched_libs:
        return None

    # Stack-filter: if the caller passed the project's detected libraries,
    # only promote a gotcha for libraries the project actually uses. This is
    # the right model — a vite project hitting "next" as an English word
    # shouldn't surface a fake Next.js gotcha to other Next.js projects.
    if project_libraries:
        project_lib_set = {lib.lower() for lib in project_libraries}
        # Also include framework keys derived by detect_stack — `nextjs` tag
        # maps to `next` package, `tailwind` tag maps to `tailwindcss`, etc.
        stack_alias_map = {
            "nextjs": "next",
            "tailwind": "tailwindcss",
        }
        for tag, pkg in stack_alias_map.items():
            if tag in project_lib_set:
                project_lib_set.add(pkg)
            if pkg in project_lib_set:
                project_lib_set.add(tag)

        relevant = [lib for lib in matched_libs if lib in project_lib_set]
        if not relevant:
            return None
        matched_libs = relevant

    # Prefer the longest matched library name (e.g. "tailwindcss" over "css"
    # if both ever overlapped). Sorted iteration above makes ties deterministic.
    lib = max(matched_libs, key=len)

    # Skip duplicates already in the store
    for g in read_gotchas():
        if g.get("library") == lib and _similar(g.get("gotcha", ""), event_summary):
            return None

    return add_gotcha(
        library=lib,
        gotcha=event_summary,
        tags=tags or [],
        source_project=project_name,
    )


def _similar(a: str, b: str) -> bool:
    """Quick similarity check — returns True if strings share 60%+ words."""
    words_a = set(a.lower().split())
    words_b = set(b.lower().split())
    if not words_a or not words_b:
        return False
    overlap = len(words_a & words_b)
    return overlap / min(len(words_a), len(words_b)) > 0.6
