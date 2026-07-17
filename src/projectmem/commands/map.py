from __future__ import annotations

from pathlib import Path

import typer

from projectmem.storage import project_map_path


def run(build: bool = False, root: Path | None = None) -> None:
    if build:
        # Extract the code-structure cache (recursive files + Python import
        # relationships). Derived from source; never touches memory.
        from projectmem.structure import write_structure

        out, data = write_structure(root)
        s = data["stats"]
        typer.echo(
            f"Built {out}\n"
            f"  {s['files']} files · {s['dirs']} dirs · "
            f"{s['relationships']} relationships (Python imports)"
        )
        return
    typer.echo(project_map_path(root).read_text(encoding="utf-8"))
