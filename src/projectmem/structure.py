"""Code-structure extractor — the DERIVED layer (never touches memory).

Walks a project's source tree and, for Python, resolves internal imports into
file→file relationships. Writes `.projectmem/structure.json`: a disposable,
gitignored cache regenerated from CODE (parallel to how summary.md is
regenerated from EVENTS). The dashboard renders the Project Map from this,
then paints the event/failure heat on top.

INVARIANT: this module only READS source and WRITES structure.json. It never
reads or writes events.jsonl / summary.md / PROJECT_MAP.md. Memory never
depends on the parser; they meet only in the renderer.
"""
from __future__ import annotations

import ast
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from projectmem.storage import require_mem_dir

# Directories that are never project structure (deps, caches, VCS, build output).
_IGNORE_DIRS = {
    ".git", ".hg", ".svn", ".projectmem", ".idea", ".vscode",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "__pycache__",
    "node_modules", "venv", "env", "virtualenv", ".venv", ".tox", ".eggs",
    "build", "dist", "target", ".gradle", "htmlcov", "coverage",
    ".next", ".nuxt", ".cache", "site-packages",
}
_CODE_EXT = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".rb", ".java",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".php", ".swift", ".kt",
    ".html", ".css", ".scss", ".md", ".json", ".toml", ".yaml", ".yml",
}
# Cap the walk so a pathological tree can't hang the extractor.
_MAX_FILES = 5000


def _walk(root: Path) -> tuple[dict, list[str]]:
    """Return (nested tree, flat list of posix-relative file paths)."""
    files: list[str] = []

    def node(dirpath: str, rel: str) -> dict:
        children: list[dict] = []
        try:
            entries = sorted(
                os.scandir(dirpath), key=lambda e: (e.is_file(), e.name.lower())
            )
        except OSError:
            entries = []
        for e in entries:
            if len(files) >= _MAX_FILES:
                break
            if e.name.startswith(".") and e.name != ".gitignore":
                continue
            if e.is_dir(follow_symlinks=False):
                if e.name in _IGNORE_DIRS:
                    continue
                sub = node(e.path, rel + e.name + "/")
                if sub["children"]:
                    children.append(sub)
            elif e.is_file(follow_symlinks=False):
                if os.path.splitext(e.name)[1].lower() in _CODE_EXT:
                    p = rel + e.name
                    files.append(p)
                    children.append({"name": e.name, "path": p, "type": "file"})
        return {
            "name": os.path.basename(dirpath) or "root",
            "path": rel or "/",
            "type": "dir",
            "children": children,
        }

    return node(str(root), ""), files


def _count_dirs(tree: dict) -> int:
    return sum(
        1 + _count_dirs(c) for c in tree.get("children", []) if c.get("type") == "dir"
    )


def _python_relationships(root: Path, files: list[str]) -> list[dict]:
    """Resolve internal Python imports into file→file 'imports' edges.

    Only edges whose target is a file IN this project are kept — stdlib and
    third-party imports are ignored (they aren't project structure).
    """
    py = [f for f in files if f.endswith(".py")]
    modmap: dict[str, str] = {}
    for f in py:
        modmap[f[:-3].replace("/", ".")] = f  # a/b.py -> a.b
        if f.endswith("__init__.py"):
            pkg = f[: -len("/__init__.py")] if "/" in f else ""
            if pkg:
                modmap[pkg.replace("/", ".")] = f

    edges: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(src: str, modname: str) -> None:
        tgt = modmap.get(modname)
        if tgt and tgt != src and (src, tgt) not in seen:
            seen.add((src, tgt))
            edges.append({"source": src, "target": tgt, "kind": "imports"})

    for f in py:
        try:
            tree = ast.parse((root / f).read_text(encoding="utf-8", errors="ignore"))
        except (OSError, SyntaxError, ValueError):
            continue
        pkg_parts = f.rsplit("/", 1)[0].split("/") if "/" in f else []
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for alias in n.names:
                    add(f, alias.name)
            elif isinstance(n, ast.ImportFrom):
                mod = n.module or ""
                if n.level and n.level > 0:
                    up = n.level - 1
                    base = pkg_parts[: len(pkg_parts) - up] if up <= len(pkg_parts) else []
                    prefix = ".".join(base + (mod.split(".") if mod else []))
                else:
                    prefix = mod
                if prefix:
                    add(f, prefix)
                for alias in n.names:  # `from pkg import submodule`
                    add(f, (prefix + "." + alias.name) if prefix else alias.name)
    return edges


def build_structure(root: Path | None = None) -> dict:
    root_path = (root or Path.cwd()).resolve()
    tree, files = _walk(root_path)
    relationships = _python_relationships(root_path, files)
    return {
        "generated": datetime.now(timezone.utc).isoformat(),
        "root": root_path.name,
        "stats": {
            "files": len(files),
            "dirs": _count_dirs(tree),
            "relationships": len(relationships),
        },
        "tree": tree,
        "files": files,
        "relationships": relationships,
    }


def structure_path(root: Path | None = None) -> Path:
    return require_mem_dir(root) / "structure.json"


def write_structure(root: Path | None = None) -> tuple[Path, dict]:
    data = build_structure(root)
    out = structure_path(root)
    out.write_text(json.dumps(data), encoding="utf-8")
    return out, data
