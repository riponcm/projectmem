from __future__ import annotations

from pathlib import Path

import typer

from projectmem.storage import plan_path


def run(add: str | None = None, root: Path | None = None) -> None:
    path = plan_path(root)
    if add:
        # Append a bullet to the Ideas section (or the top if it's missing).
        # Everything richer — checking off, moving to Shipped, reordering — is
        # done by the AI editing plan.md directly; this is a human convenience.
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        line = f"- {add.strip()}\n"
        if "## Ideas\n" in text:
            text = text.replace("## Ideas\n", "## Ideas\n" + line, 1)
        else:
            text = text.rstrip() + "\n\n## Ideas\n" + line
        path.write_text(text, encoding="utf-8")
        typer.echo(f"Added to Ideas in {path}")
        return
    typer.echo(path.read_text(encoding="utf-8"))
