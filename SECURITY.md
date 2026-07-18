# Security Policy

projectmem is local-first by design — no telemetry, no cloud, no accounts. Your project memory never leaves your machine unless you explicitly send it to an AI tool (Claude, Cursor, etc.).

## Reporting a Vulnerability

If you've found a security issue, please **email support@projectmem.dev** with details rather than opening a public issue.

- We aim to respond within 72 hours.
- We'll work with you on disclosure timing.
- Significant fixes will be credited in the changelog (unless you prefer anonymity).

## Honest Trade-offs

projectmem is open source and we'd rather be transparent about the trade-offs than hide them. The most up-to-date discussion is in the [Privacy & Security section of the User Guide](https://projectmem.dev/guide#privacy-security). The short version:

### Where data lives

| Location | Contents | Shared? |
|----------|----------|---------|
| `.projectmem/events.jsonl` | Raw event log (failures, fixes, decisions) | Gitignored by default |
| `.projectmem/summary.md` | Distilled briefing for AI agents | Committed to git |
| `.projectmem/PROJECT_MAP.md` | Architecture map | Committed to git |
| `~/.projectmem/global/` | Cross-project patterns and gotchas | Local only, shared across all your projects |
| `$PROJECTMEM_HOME/projects.json` (default: `~/.projectmem/projects.json`) | Registered project IDs, aliases, absolute local paths, brains, tags, and active selection | Local only |
| `.projectmem/watch.pid` · `watch.log` | Watcher state and runtime log | Gitignored, local only |

The project registry contains local filesystem paths. It remains on your machine; projectmem adds no telemetry or network transfer for registry data.

### Honest trade-offs

1. **Never paste secrets into `pjm log/note/decision`.** The event log is append-only. Treat it like git history: don't commit anything you wouldn't want re-read later.

2. **Local-first ≠ data stays on your machine.** projectmem doesn't see your data. But the moment you connect it to Claude, Cursor, ChatGPT, or any cloud AI, your `summary.md` / `PROJECT_MAP.md` contents are sent to that AI vendor as part of normal AI use.

3. **Global memory mixes work and personal projects.** `~/.projectmem/global/` is shared across every project on your machine. Use `pjm init --no-global` for sensitive repos.

4. **Git hooks execute `pjm` on every commit.** `pjm init` installs three git hooks (pre-commit, post-commit, post-merge). Inspect them in `.git/hooks/`. Remove with `pjm hooks uninstall`.

5. **The watcher is a detached background process.** `pjm watch --daemon` auto-starts on `pjm init` in interactive terminals. Stop with `pjm watch --stop`. Skip auto-start with `pjm init --no-watch`.

6. **Memory files are AI instructions — treat like code.** AI agents read `AI_INSTRUCTIONS.md`, `PROJECT_MAP.md`, and `summary.md` as authoritative guidance. Malicious prompt-injection text in those files can manipulate AI behavior. Review changes like you'd review code.

7. **The MCP server is local-only.** `pjm-mcp` listens on stdio, not over the network. No remote attack surface — but any local AI client you connect can write to your memory via the 8 MCP tools.

## Fully Uninstall

```bash
pjm watch --stop          # 1. stop the watcher
pjm hooks uninstall       # 2. remove git hooks
rm -rf .projectmem/       # 3. delete project memory (optional)
rm -rf ~/.projectmem/     # 4. delete global memory (optional)
pip uninstall projectmem  # 5. uninstall the package
```

## Scope

The following are **in scope** for security reports:

- Code execution vulnerabilities in `pjm` CLI or `pjm-mcp` server
- Information disclosure beyond what's documented in the trade-offs above
- Privilege escalation via git hooks
- Malicious-package risks via dependency chain (we use only `typer`, `mcp`, `watchdog`)

The following are **not** security issues (by design):

- AI vendor receiving your memory when you connect to them — that's normal AI use
- Global memory being shared across local projects — that's the documented behavior; use `--no-global` to opt out
- Git hooks running `pjm` — that's the documented behavior; uninstall hooks to opt out
- Sensitive data appearing in `events.jsonl` because you typed it — see trade-off #1
