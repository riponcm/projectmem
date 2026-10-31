# Changelog

## 0.3.5

**Two community fixes: precheck recognises a file however its path is spelled, and the summary stays small even when entries are long.**

### Fixed

- **`precheck_file` called a file with known failures "Safe to modify" when its path was spelled differently.** Events store project-relative paths, so `./src/auth.py`, an absolute path, a path through a symlinked folder, a backslash path or a Windows `C:\` path matched nothing. Every path, the one being checked and the ones in the log, is now made project-relative before matching, and two spellings of one file give one set of warnings. A decision logged at an absolute path is now also judged stale for the right file. Matching got faster as well: checking 5 files against a 3,000-event log went from 663 ms to 163 ms. ([#26](https://github.com/riponcm/projectmem/pull/26))

- **`get_issue` missed unpadded ids and passed raw input to a file glob.** `get_issue("1")` and `get_issue("#1")` now find issue 0001, as `pjm fix` does, and backfilled `legacy_…` ids work. Input containing `*`, `?` or `[` is refused instead of matching some other issue. ([#26](https://github.com/riponcm/projectmem/pull/26))

- **`summary.md` could still cost ~9,000–12,000 tokens on an aged project.** 0.3.4 capped how many entries the summary shows, but not how long each one is. Each record is now clipped at 300 characters, with line breaks folded first and locations kept whole. A fixed issue is one line (id, title, location), and its details are one `get_issue` call away. Open issues still show their last 3 failed or partial attempts. On a real 238-event project, the summary went from 8,974 to 3,458 tokens. ([#29](https://github.com/riponcm/projectmem/issues/29), [#30](https://github.com/riponcm/projectmem/pull/30))

- **`precheck_file("")` reported the whole log.** An empty string is part of every summary, so a blank path matched every failed attempt and open issue. Blank paths are now ignored, and whitespace around a path is trimmed.

- A whitespace-only location rendered as `[]` in the summary and hid the fix's location. It now counts as no location.

### Changed

- **`AI_INSTRUCTIONS.md` asks for shorter records:** issue titles under 140 characters, everything else under 300. The five write tools say the same in their argument descriptions. Because the template changed, run `pjm init` once in each project. It refreshes the file and keeps your previous copy as `AI_INSTRUCTIONS.md.bak`, so merge back anything you added by hand.

- **Token figures corrected.** The guidance said `get_summary` costs "a few hundred to ~1,500 tokens". That was measured on short synthetic entries, and a real project with long entries was far above it ([#29](https://github.com/riponcm/projectmem/issues/29)). It now says a few hundred on a young project and a few thousand on an aged one. The README's per-session estimate also left out `get_instructions`, which agents read first in every session and which is about 3,300 tokens on its own. The table now counts it and gives measured figures: on nine real projects, the three files read at session start total about 3,700 to 7,600 tokens.

The fixes for #26 were contributed by [@vishalhabib99](https://github.com/vishalhabib99), and #29 was reported and fixed by [@bigfree](https://github.com/bigfree).

## 0.3.4

**Three reports, three community fixes, and six more bugs found by checking them.** Each report below was reproduced end to end before it was fixed, and each fix was reviewed by someone other than its author.

### Fixed

- **The guidance named arguments the tools reject.** `AI_INSTRUCTIONS.md`, the `CLAUDE.md`/`AGENTS.md` bridge and the MCP server's own instructions said `precheck_file(path)`; the argument is `file_path`, so an agent that copied the call got a validation error. The README had the same mistake for `get_issue(id)` (it is `issue_id`). A test now reads every call shown on every surface and checks it against the tool's real signature, so a rename can no longer leave the docs behind. ([#18](https://github.com/riponcm/projectmem/issues/18), [#22](https://github.com/riponcm/projectmem/pull/22))

- **`summary.md` grew without limit.** Notes were capped at 10, but Recent issues and Decisions listed everything ever logged, so on an aged project the summary cost more to read than the code it describes. Every **open** issue is still listed — it is the one thing the next session must not miss — while fixed issues are capped at the newest 10 and decisions at the newest 15, each with a line saying how many were left out and where to find them. Issue ids sort as numbers, so ids past 9999 no longer sort the newest to the bottom. Past 20 open issues, the older ones are listed by id on a single line instead of in full, so a neglected backlog cannot regrow the summary either. ([#19](https://github.com/riponcm/projectmem/issues/19), [#25](https://github.com/riponcm/projectmem/pull/25))

- **Auto-captured commits were filed under `.projectmem/summary.md`.** The location was the first changed path in git's sort order, and `.projectmem/` sorts first, so most commits pointed at the regenerated summary; the staleness check then called valid memories stale and the real file lost its history. The location is now the file the commit is about: deleted paths are skipped, renames use the new name, the first commit and merge commits list their files, non-ASCII paths are unquoted, and source code is preferred over tests, docs, build files, assets and lockfiles. ([#20](https://github.com/riponcm/projectmem/issues/20), [#23](https://github.com/riponcm/projectmem/pull/23))

- **Staleness depended on the time zone — a regression in 0.3.3.** The 0.3.3 speed-up compared git's local-offset commit times with UTC event times as plain strings. West of UTC, recent stale memories were never flagged; east of UTC, fresh ones were flagged at once. Both sides are now compared in UTC.

- **Already-polluted logs stop warning without being rewritten.** Events filed under `.projectmem/` before this release are no longer stale-checked, and neither are auto-captured fix and note records — they record what a commit did, which cannot go stale. Auto-captured decisions (`Refactor:`, `Breaking change:`) still are. Nothing to run.

- **One mistyped issue id broke every later write.** `pjm attempt --issue 0099` (or `record_attempt` with a wrong `issue_id`) appended an attempt with no issue behind it, and the summary rebuild after every following command raised `StopIteration`. Unknown ids are now refused before anything is written, ids are normalised the way `pjm fix` does it, a stale active-issue marker is ignored and cleared, and the summary skips a malformed group instead of crashing.

- **`pjm precheck` warned about projectmem's own files.** Staging the regenerated summary printed "HIGH CHURN … may indicate an unresolved architectural issue". Memory files are left out of the check, and `precheck_file` on one now says to use the log tools instead of calling it safe to edit.

- **Fixes to the git hooks never reached existing projects.** `pjm init` skipped any hook it had already installed, so the 0.3.3 hook fixes for Windows and GitHub Desktop only reached new projects. Re-running `pjm init` now refreshes projectmem's block in place and leaves everything else in the hook untouched. The hook also records the `pjm` that ran `init`, not whichever one PATH finds first.

### Changed

- **`pjm init` refreshes an outdated `AI_INSTRUCTIONS.md`.** It was written only when missing, so every existing project kept the guidance of the release that created it — including the wrong argument above. Re-run `pjm init` once: the file is brought up to date, your previous copy is saved as `AI_INSTRUCTIONS.md.bak` (gitignored), and your inherited global-memory block is kept. A file already at the current version is never touched.

- **The guidance states what reading memory costs.** It said a `get_summary` call costs ~500 tokens. Measured on the capped summary it is about 250 tokens on a young project and about 1,100 on an aged one, more with many open issues, so it now says "a few hundred to ~1,500".

- `pjm init` no longer writes `summary_size_limit_kb` to `config.toml`. Nothing ever read it.

- **After upgrading, the CLI says to run `pjm init` in each project**, since that is what refreshes the instructions file and the git hooks.

### Docs

- The README, `llms.txt`, `SECURITY.md` and the tutorial were checked line by line against the code. Fixed: the MCP tool count (17, not 15 or 8), links that broke on PyPI, a link to a contributing guide that did not exist, an example `precheck` line the command never prints, token figures now labelled as estimates, the client-workspace-root routing step marked as planned (it is not wired up yet), and claims that no other tool offers pre-commit warnings or keeps superseded memories — the comparison table was rebuilt from each project's own docs as of 2026-10-02.

Fixes for #18 and #20 were contributed by [Rohan Tiwari](https://github.com/RTE404) and the first cap for #19 by [hylin](https://github.com/linhongyu510). #18 and #19 were reported by [@medium-effort](https://github.com/medium-effort) and #20 by [@bigfree](https://github.com/bigfree).

## 0.3.3

**Three reports from Windows, and the tool no longer freezes as your project grows.** Every fix below was found by running projectmem on a real machine, not by reading the code.

### Fixed

- **`pjm watch --daemon` could not be seen or stopped on Windows, and leaked a process per run.** A hook whose shebang cannot be resolved does not get skipped — git aborts the commit. The hooks shipped `#!/usr/bin/env bash`, which depends on PATH; Git for Windows maps `/bin/sh` to its own shell, and under GitHub Desktop bash often is not there. The snippet body only ever used `[ ]`, `command -v` and `$( )`, so bash bought nothing. Two more faults fed the same failure: the baked binary path went in raw, and on Windows the shell ate its backslashes (`C:\Users\ripon` became `C:\Usersipon`), so the `-x` test failed and the hook silently fell through to its PATH lookup — it only ever worked by accident; and the venv fallback looked in `<prefix>/bin/pjm`, which cannot exist on Windows. ([#16](https://github.com/riponcm/projectmem/issues/16))

- **`get_context` described the wrong directory.** `generate_context` defaults its root to `Path.cwd()`, and the MCP server was the only caller that omitted it — both CLI call sites pass it. For an MCP server, cwd is wherever the client launched the process, so the git status and architecture came from the agent harness rather than your project. ([#15](https://github.com/riponcm/projectmem/issues/15))

- **A decorative character killed the command on a Windows console.** `pjm init` created `.projectmem/`, the git hooks, `CLAUDE.md` and `AGENTS.md`, then raised `UnicodeEncodeError` printing a box-drawing rule and exited 1 — so the command both did its work and reported failure, which is the worst outcome for any script checking the exit code. A stock Windows install runs the console on cp1252, where `U+2550` has no mapping, and writing it raises rather than degrades. The reported line was only the first one reached: 11 of the 14 glyphs projectmem prints to a terminal are unencodable there. Output streams are now set to replace what they cannot encode, so no glyph can abort a command, and the glyphs themselves fall back to ASCII when the stream cannot carry them — cp1252 output reads as deliberate rather than as a row of `?`. UTF-8 terminals are unchanged, and the web dashboard keeps its real characters.

- **Prose punctuation came out as black diamonds on the same console.** A second report against the fix above. An em dash *is* cp1252-encodable (`0x97`), so neither the ASCII fallbacks nor the replacement net touched it — and it still rendered wrong, because Python wrote `0x97` and a terminal set to UTF-8 read that as an invalid lead byte and drew `U+FFFD`. Encodable was never the right test. On a stream that cannot carry Unicode, projectmem now emits no non-ASCII byte at all, so nothing downstream has anything to misread.

- **`precheck_file` invented warnings about every project but the server's own.** The tool resolved the project root to read events, then called `_analyze_files` without it, so staleness asked "does this cited file exist?" relative to the MCP server's working directory. In global mode that is the agent harness, so a four-file precheck on an untouched repo came back with 373 phantom "cited file no longer exists" warnings. `_analyze_files` had the same gap one level down — it took the root but called `_git_recent_changes` without it, so `git log` ran in the wrong repository, and outside a repository git fails and the churn count silently fell back to the event log, reporting churn the project did not have. Both surface only when the root differs from cwd, which is every MCP client that does not chdir into the project and never the CLI — which is why `pjm precheck` was always correct.

- **Expected errors were reported as crashes.** `pjm fix` with no open issue printed a clear message and then a full traceback underneath it. `typer.Exit` escaped `main()` uncaught, so Python printed the chained cause.

### Faster

- **`pjm precheck` was taking 26 seconds on a 1,200-event project, and getting worse as the project aged.** It ran one `git log` per event — 1,201 subprocesses — to answer a question about a single file, and the instructions tell agents to call it before modifying anything. Three faults compounded: staleness was computed across the whole event log and then filtered down to the file being checked; the memo key was `(file, timestamp)`, which looks right but never hits because real events carry distinct timestamps; and nothing bounded the git walk. It now makes one `git log` per distinct file, bounded by the oldest event citing it, and counts in memory.

  | events | before | after |
  |---|---|---|
  | 100 | 2,210 ms | 48 ms |
  | 400 | 8,905 ms | 51 ms |
  | 1,500 | ~33 s, 1,501 git processes | **82 ms, 2 processes** |

  Latency is now flat rather than linear in project age. The project-wide path used by `pjm brief` and the dashboard went from 26,294 ms to 450 ms. Results are unchanged — verified against a reimplementation of the old algorithm, including across merge commits.

### Changed

- **Your project is named in `CLAUDE.md`.** One server serves every project, so a tool call that names none is refused rather than guessed at — but nothing ever told the agent the name, so it learned it from the error and retried. That round trip happened every session. The bridge now carries the registered name and the exact argument to pass. ([#14](https://github.com/riponcm/projectmem/issues/14))

- **`AGENTS.md` is written alongside `CLAUDE.md`.** The 0.3.3 fix for #14 put the project name in `CLAUDE.md` — which Antigravity and Codex never read, so for those clients the fix did not exist. `pjm init` now writes both files with identical content, and the guidance no longer asks the agent to look the project up before starting: it names the active project first and says calls that omit `project` land there. ([#14](https://github.com/riponcm/projectmem/issues/14))

- **Retiring a decision is discoverable.** `supersedes` has existed since 0.1.4 and the summary renderer honours it, but `AI_INSTRUCTIONS.md` mentioned it zero times in 12,503 characters, so models never called it and `summary.md` accumulated decisions that contradicted each other. It is documented now on all three surfaces an agent may read, and each says what it does to the summary rather than only that the argument exists. ([#17](https://github.com/riponcm/projectmem/issues/17))

Windows daemon support in 0.3.2 was contributed by [@medium-effort](https://github.com/medium-effort), who also reported every issue fixed in this release.

## 0.3.2

**Windows works properly now.** `pjm watch --daemon` crashed there, and fixing the crash uncovered a second bug one function away that had been hiding behind it. Both are fixed. `pjm doctor` also learned to notice when a fix you made gets undone.

### Fixed

- **`pjm watch --daemon` crashed on Windows** with `AttributeError: module 'os' has no attribute 'fork'`. Daemonisation used the POSIX-only `os.fork()` and `os.setsid()`; it now spawns a detached worker with `subprocess.Popen` — `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW` on Windows, `start_new_session=True` elsewhere. Contributed by [@medium-effort](https://github.com/medium-effort) ([#12](https://github.com/riponcm/projectmem/issues/12), [#13](https://github.com/riponcm/projectmem/pull/13)).

- **The watcher could not be seen or stopped on Windows, and leaked processes.** `_running_pid()` probed liveness with `os.kill(pid, 0)`. That is the POSIX idiom and does not port: on Windows `os.kill` routes to `TerminateProcess`, so signal 0 is not a liveness check and fails whether or not the process exists. `pjm watch --status` always said "not running", the PID file was deleted as stale while the worker ran on, `--stop` found nothing to stop, and every further `--daemon` slipped past the already-running guard and started another one. Liveness now uses `OpenProcess` + `GetExitCodeProcess` on Windows, and access-denied counts as alive — a process you cannot open still exists, and calling it dead is what orphans it. This bug predates the crash above and was only invisible because nothing got far enough to hit it.

- **`pjm doctor` reported a clean bill of health on a Store-installed Claude Desktop.** A Microsoft Store (MSIX) install runs in an AppContainer, where the app's writes to `%APPDATA%` are redirected into the package's `LocalCache`. Inside the container the config still appears at `%APPDATA%\Claude`; from any ordinary process it does not exist there at all. Doctor found nothing and printed green while two servers pinned with `--root` were running — worse than missing the problem, because it ends the investigation. Store installs are now discovered under `%LOCALAPPDATA%\Packages\Claude_*` and labelled **Claude Desktop (Store)**, since the file is at a path most users have never seen. Found on Windows against a 0.3.2 build.

- **Client config paths now read `%APPDATA%` and `%LOCALAPPDATA%`** rather than assuming they resolve under the home directory. The old assumption is wrong on any machine with a redirected profile — roaming profiles, OneDrive Known Folder Move, most managed corporate setups.

- **A config that cannot be read is no longer counted as fine.** Unreadable configs — permissions, a lock, a broken link — were skipped silently, which made "I could not look" indistinguishable from "I looked and it is fine". They now get their own line.

- **`pjm doctor` also checks the project you are standing in.** A per-repo `.mcp.json` or `.cursor/mcp.json` pins just as effectively as a global config, and was never examined.

- **`--at src/foo.py` counted for nothing.** Every place that turns an event location into a file path used `location.split(":")[0]`, which had two faults. A bare path has no colon, so `pjm score`'s file-coverage component ignored it — up to 20 points lost in silence, and weaker churn detection in `pjm precheck` — even though `--at`'s own help text invites that form. And a Windows path carries a drive colon, so `C:\src\foo.py:42` resolved to a phantom file called `C`. Both are fixed in one shared parser now used by score, precheck, staleness, brief, context and export. `--at src/foo.py` and `--at src/foo.py:42` finally count the same, and a `ClassName.method` location is still correctly not a file.

- **Global memory ignored `$PROJECTMEM_HOME`.** `global_dir()` read a constant fixed at import from `Path.home()`, while the registry resolved the environment variable per call. Since `append_event()` auto-promotes library mentions into the global store, a test or fixture running under an isolated home still wrote invented gotchas into the real `~/.projectmem/global/`. It now resolves per call, the same way the registry does.

- **Two tests only passed on POSIX.** One asserted a raw path inside JSON output where the product correctly emits escaped backslashes; the other set `$HOME`, which Windows ignores in favour of `%USERPROFILE%`, so the fixture home went unused and the real profile was scanned. All nineteen `$HOME` fixtures now go through one portable helper.

### Changed

- **`pjm doctor` tells you to quit the client before editing its config.** These files hold the app's own preferences too, so a running client can rewrite the whole file from the copy it loaded at startup and restore the `--root` you just removed. Doctor was giving advice that silently fails while the app is open.

- **`pjm doctor` notices when a fix has been undone.** It remembers the previous verdict for each client config, so a config that was clean last run and is pinned again now gets named, with the time it was last clean — instead of looking flaky for reporting a problem you know you already fixed. It records every config on disk that mentions projectmem, including the ones that are already fine, so the very first run after upgrading is enough to catch a clobber straight afterwards. Local files only; no process inspection, and nothing leaves your machine.

## 0.3.1

**Know when there is something to upgrade to — without giving up the promise.** projectmem says it makes no network calls, and that is a reason people choose it. So update checking is opt-in everywhere, and nothing about your machine is ever sent: the request is a plain GET of the same public JSON file `pip install` reads.

### In the dashboards

Both `pjm visualize` and `pjm dashboard` now show which version generated the page — the project dashboard in the sidebar under the star button, the global one in the footer. Beside it is a **check for updates** link. The page makes no request until you click it, and the result is one of *up to date*, *X available ↗* (linking to PyPI), or *could not reach PyPI*, which changes nothing else on the page.

### On the command line

```bash
pjm doctor            # shows your version and how to check
pjm doctor --online   # one-off check
pjm doctor --auto     # remember to check, at most once a day
```

`--auto` is off by default and can be turned off again with `--no-auto`. When on, the result is cached, so an enabled check costs one request a day.

### Fixed

Version comparison stripped every non-digit, which turned `1.0.0rc1` into `(1,0,1)` and sorted a release candidate *above* the final release — someone on `1.0.0` would have been told to upgrade to `1.0.0rc1`. Leading digits only now.

## 0.3.0

**One server, many projects.** Until now an MCP config was tied to one repository: eleven projects meant eleven server entries and eleven restarts. 0.3.0 serves every registered project from a single server — paste the config once, and every repo you `pjm init` afterwards is reachable from it. Still local-first: no central store, no telemetry, no cross-project memory retrieval. The event schema is unchanged.

### New: global MCP mode

Every repo tool takes an optional `project` argument (a registered id, alias, or path), and two new tools — `list_projects` and `current_project` — let an agent see the names and check where a write would land before making it. 17 tools total.

Resolution happens per call and is never cached, in this order: a pinned `--root`, then an explicit `project`, then the client's workspace root, the active project, the working directory, and finally the sole registered project when there is only one. When none of those apply it raises an error listing what is registered — it never guesses.

Two properties worth knowing:

- **A pinned `--root` is a boundary, not a default.** A server configured for one repository refuses to write anywhere else, even when a call explicitly names another project. Existing 0.2.x configs keep working exactly as before.
- **Every write names the project it landed in.** In a single-project setup a misconfigured server simply fails; a shared server can succeed against the *wrong* repository, which corrupts two audit trails at once. The echo makes that visible on the call that caused it.

### New: the project registry and `pjm project`

`~/.projectmem/projects.json` grows from a list of paths into schema v1 records with ids, aliases, tags and an active selection. **Migration is automatic** on first read, in place, keeping a `.bak`. `pjm dashboard` and `pjm init` are unaffected.

`pjm project list | register | use | alias | tag | remove` manage it. Removing a project from the registry never touches its repo or its `.projectmem/`.

### Fixed: the MCP server was broken on fresh installs

mcp 2.0.0 (2026-07-28) renamed `FastMCP` to `MCPServer` and left `mcp.server.fastmcp` behind as a module that raises on import. With an unpinned `mcp>=0.1.0`, every `pip install projectmem` after that date got a server that died at import — markdown mode kept working, the tools did not. Reported and fixed by [@VIVAAN-DHAWAN](https://github.com/riponcm/projectmem/pull/10); `pyproject.toml` now declares `mcp>=1.0,<3`.

### Fixed: stored XSS in `pjm visualize`

Event summaries reached the DOM unescaped. Since `pjm`'s auto-capture turns git commit messages into event summaries, a crafted commit in a branch you pulled could execute script in your dashboard — a page that embeds your entire event log. Every sink is escaped now, and a `</script>` in an event no longer breaks the page.

### The dashboard, rebuilt

`pjm visualize` opens on a shareable **Memory Card** (grade, tokens and hours saved, downloadable as a PNG) above **Case files** — every problem with its full issue → attempt → fix chain, one click from the Timeline or the Story Map. The Story Map gains an effort treemap and story lanes alongside the force graph, and any file opens a **dossier**: `precheck_file`, rendered.

`pjm dashboard` now opens with **where you left off** — your last working session reconstructed from event timestamps, what it was, and what it left behind — plus an attention band across every project and a state badge per card.

### Also

- `pjm init` prints the shared MCP config by default; `--mcp-config-single` prints the pinned form. It also warns when an existing client config still pins projectmem to one repo.
- `pjm dashboard` honours `$PROJECTMEM_HOME` for its output directory, so a sandboxed run no longer overwrites the real dashboard.

## 0.2.0

**The workspace release: memory grows from one project to your whole machine — and finally meets your code's real structure.** 0.1.6 made a single project's memory something you could watch; 0.2.0 lifts that to every project at once, and closes the gap between *what happened* (memory) and *what the code is* (structure). Still local-first: no central store, no telemetry, no new required dependencies, and the event schema is unchanged — the six core event types (issue, hypothesis, attempt, fix, decision, note) are exactly as before.

### New: `pjm dashboard` — the cross-project global view

One page over every project you've `pjm init`-ed: total issues captured, fixes confirmed, dead-ends prevented, tokens/dollars saved, a grade per project, issues-over-time, and a "needs attention" list. Click any project card to open **that repo's own dashboard**, generated fresh from its current files, with a "← All projects" link back.

Crucially this is a global **view, not a global store.** Nothing is centralized — the dashboard aggregates each repo's own `.projectmem/` at read time and never copies memory out of its folder. A tiny registry at `~/.projectmem/projects.json` just records *where* your projects are (override with `$PROJECTMEM_HOME`).

- **Default is serverless** — `pjm dashboard` writes a self-contained static snapshot and opens it. Re-run to refresh. The page labels itself a *snapshot* so it never overclaims.
- **`--serve` is live** — `pjm dashboard --serve` runs a tiny, ephemeral local HTTP server: the Refresh button re-reads your files, and each project card renders on demand from its latest state. No background daemon — it exists only while the command runs, and **Ctrl+C** (or `kill`) stops it cleanly. `--port` to choose the port.

### New: code structure & relations — `pjm map --build`

Memory knew your *history*; now projectmem also reads your *structure*. `pjm map --build` (run automatically after `pjm init`) walks the codebase and, for Python, resolves imports into a real dependency graph, written to a **derived, gitignored** `.projectmem/structure.json` cache — code is only ever read, never modified. The Project Map's **Graph** and **Flow** views now render actual files and the import edges between them, not just files that happen to appear in the event log.

### New: failure heat on the structure graph (the combo)

The one view neither a pure code-grapher nor a pure memory tool can draw: real structure with real judgment on top. Files with repeated failed attempts in your event log glow red — with a dashed ring past a threshold — laid directly over the structure/import graph, and hovering shows the failed attempts. Structure comes from the code, heat comes from memory, and they meet only in the renderer.

### New: `plan.md` — an editable intent file, separate from memory

A new `.projectmem/plan.md` scaffolded at init: **ideas and plans — what you *mean* to do**, with Ideas / Active plans / Next / Someday / Shipped sections. It is deliberately **not** the event log: `events.jsonl → summary.md` records what *happened*; `plan.md` records what you *intend*. The AI reads it at session start and edits it directly (like `PROJECT_MAP.md`); a plan never becomes an event. `pjm plan` prints it, `pjm plan "idea"` appends one, and the MCP `get_plan()` tool exposes it. Committed (not gitignored) so intent is shared with the team.

### Also

- New tests cover the registry, the global dashboard (static + `--serve`), the structure extractor, and the plan.md feature (135 total).
- The structure cache (`structure.json`) is added to the consumer `.gitignore` automatically — it is derived from code and never needs committing.

## 0.1.6

**The visualization release: your project's memory is now something you can watch — and share.** The dashboard grows from four tabs to six, every new view is rendered from the same real event log, and nothing new is required: zero new dependencies, no schema changes, no CLI changes. Includes the Story Map readability controls contributed by @hanley-development (#7, which also folds in #6 — thanks!).

### New: Showoff — animated story scenes with a built-in recorder

A new dashboard tab with three animated scenes, all driven by your real events: **Story Replay** (the project's history builds itself node by node on a force canvas — auto-loops, scrubber, live captions), **Orbit** (files orbit the project, events orbit their file), and **Universe** (the project as a rotating spiral galaxy; real events blink as bright stars). Click any node for its real details — the field dims, a halo pulses, and its connections light up; click again to release. Play/pause and speed controls throughout, and the animation only runs while the tab is open.

**REC 10–60 s** records the stage via the browser's own MediaRecorder and downloads a `.webm` — rendered 100% locally, with a toggleable *"made with projectmem"* badge baked into the video. Recording stops safely if you navigate away. (X/Twitter prefers mp4; most other platforms accept webm directly.)

### New: Flow — the Project Map's default view

A layered left-to-right flowchart of the project story: **PROJECT → DIRECTORIES → FILES → WHAT HAPPENED → MEMORY.** Files with 3+ failures are outlined red with red flow lines, every file carries its outcome chips (failed / fixed / decisions / notes), and everything flows into an `events.jsonl` cylinder — the append-only memory, drawn as what it is. Zoom and pan like the other views; auto-fits on open. Tree and Graph remain one click away, and a new **Hide details** toggle collapses the PROJECT_MAP.md pane to give any view the full width. No code parsing — it is a pure layout over the same event graph the Story Map uses.

### New: Time Spine — the Timeline's default view

A central real-time axis you scroll: day pills, *"Nh quiet"* gap markers, and the story branching into two meaningful sides — **problems on the left** (issues; failed / partial / worked attempts), **knowledge on the right** (fixes, decisions, notes). Hover any card and its entire issue thread stays lit while everything else dims — you can watch one bug's story thread down through time. The classic list view remains as **Details**.

### From PR #7 (@hanley-development): Story Map readability controls

Collapse dense files (10+ events) into a draggable bubble, collapse directories with click-to-drill-down, expand all, and reset focus. Label noise reduced; file-focus mode dims unrelated nodes; file-only locations (no `:line`) now link correctly (folds in #6). Windows-style paths normalized.

### Also

- README gains a "New in 0.1.6" showcase with dashboard screenshots.
- 8 new tests pin the new template surface (115 total).

## 0.1.5

**A reliability release: same features, fewer ways to break.** Three community fixes (thanks @hanley-development) make projectmem safer to run everywhere — you can now close a *specific* issue by ID, MCP git calls can't hang a stdio session, and `pjm brief` / `pjm precheck` no longer crash on non-UTF-8 Windows consoles. No new dependencies, no schema changes.

### Fixed: `pjm fix --issue` / `record_fix(issue_id=…)` — close the issue you mean

`pjm fix` and the MCP `record_fix` previously closed whatever issue was "current," so fixing an older issue after a newer one was logged could silently close the wrong one and write that wrong link into the append-only log. You can now target a specific issue: `pjm fix --issue 0001 "…"` (IDs normalize: `1`/`001`/`0001`), and the active-issue marker is cleared only when it matches. Plain `pjm fix` is unchanged. Closes #3.

### Fixed: MCP git helpers no longer hang stdio sessions

Some MCP tool paths spawned git subprocesses that inherited the server's stdin — which, in a stdio MCP session, is the JSON-RPC transport itself, so a child could block the whole server (notably on Windows). Git calls now run with detached stdin (`DEVNULL`) and bounded timeouts. CLI behavior is unchanged; adds stdio regression tests.

### Fixed: console output is encoding-safe

`pjm brief` and `pjm precheck` print emoji and box-drawing characters that crash on Windows cp1252 consoles and git hooks (`UnicodeEncodeError`). Output is now sanitized to the active console encoding (UTF-8 unchanged; limited consoles degrade gracefully), and separators/markers fall back to ASCII. Decorative output can no longer abort a command or a hook.

## 0.1.4

**The accountable-judgment release: memory that flags its own staleness instead of silently trusting (or deleting) it — plus a dashboard that opens on an all-at-a-glance Overview.** Six small features (~150 lines, no new dependencies, no schema breaks) sharpen what makes projectmem different: it never deletes a memory, it tells you when one may have gone stale, it lets you retire decisions without losing history, it lists what already failed before you try it again, it briefs you at session start, it snoozes politely when it's wrong, and it exports its judgment to CLAUDE.md for agents that don't speak MCP. Also bumps the version (the `__init__.py` / `pyproject.toml` mismatch is corrected to a single `0.1.4`).

### New: stale-memory detection — flag, never delete

Other memory tools silently decay or delete old memories (and collect bug reports about wanted memories disappearing). projectmem now does the opposite: every decision/fix/note that cites a file is cross-referenced against that file's git history, and when the file has changed substantially since the memory was logged (3+ commits, or the file no longer exists), `pjm precheck` and the MCP `precheck_file` tool flag it — *"decision [evt_…] predates 4 commits to auth.py — confirm it still holds, or retire it"*. Nothing is hidden, nothing is removed; a human (or agent) decides. Deterministic `git log` counts; no embeddings, no daemon.

### New: superseded-decision marking — retire without rewriting history

`pjm decision "switch to argon2" --supersedes <event-id>` (also on the MCP `add_decision` tool) records a new decision that retires an old one. The old event stays physically in `events.jsonl` (append-only, always), drops out of `summary.md`, and shows up in `pjm search` tagged `(superseded)`. Search output now prints event ids so the reference is one copy-paste away. A bad reference fails *before* anything is written. Together with stale detection this completes the non-destructive answer to memory decay: **detect staleness → supersede explicitly → never lose history.**

### New: `pjm brief` — the session-start briefing

One screen that answers "where was I?": active failure warnings by file, possibly-stale memories, open issues, the latest live decisions, stack-relevant gotchas from global memory, and the prevention score with a week-over-week delta. Composes data the other commands already compute; runs in milliseconds; nothing leaves the machine.

### New: precheck snooze — the polite off-switch

`pjm precheck --snooze 2h` (forms: `30m`, `2h`, `1d`) silences pre-commit warnings for a bounded window instead of pushing annoyed users to `--no-verify` (which silences forever and leaves no trace). The snooze is itself logged to memory — even the silence is audited — and while active, every commit prints one dim line saying warnings are snoozed, so a silenced warning is never mistaken for a clean check. `--unsnooze` restores warnings early; expired markers clean themselves up.

### New: failed approaches listed in precheck output

The pre-commit warning (and MCP `precheck_file`) now lists the dead ends themselves — *"What already failed here: ✗ tried CSS contain:layout (2w ago) ✗ debounced the handler (2w ago)"* — instead of just a count. The data was always in the log; now it's at the decision point. `pjm search --failed-only` lists the project's full catalogue of dead ends.

### New: `pjm export --claude-md` — judgment for agents without MCP

Compiles live memory — current decisions (with stale flags), known gotchas, and a "Do NOT retry — these already failed" section — into a marked, auto-regenerated block inside `CLAUDE.md` (`--cursor` also writes `.cursorrules`; `--stdout` previews). Any agent that reads the file inherits the project's judgment with zero MCP setup. The block is replaced in place on re-run; the rest of the file — including the `pjm init` MCP bridge block — is never touched. Superseded decisions are excluded; possibly-stale ones are flagged, not hidden.

### Fixed: walk-up discovery no longer mistakes the global store for a project

Running any `pjm` write command from a directory under `$HOME` with no initialized project used to walk up, land on `~/.projectmem/` (the machine-wide **global store**), misread it as project memory, and silently accrete events into it — then crash on commands that expected an `issues/` directory. Discovery (both the CWD check and the walk-up) now only accepts an *initialized* project dir — one containing the `config.toml` that `pjm init` always writes and the global store never has. Found by dogfooding 0.1.4 on this very repo.

### Also fixed (caught by the 0.1.4 testing playground)

- `pjm precheck payment.py auth.py` — checking *named* files now works from the CLI. The module docstring had advertised a `--files` option that was never wired up; files are now a positional argument (staged files remain the default).
- `pjm search payment.py` now matches the **`location`** field, so per-file lookups behave like precheck: attempts logged with `--at payment.py` are findable by filename (previously search only scanned summary/notes/files).

### Event schema

One new **optional** field: `supersedes` (event id) on decision events. Existing logs parse unchanged; older projectmem versions ignore the field. The 14-tool MCP surface is unchanged (`add_decision` gained an optional parameter; `search_events`/`precheck_file` outputs got richer). 23 new tests (81 total).

**`pjm visualize` gets an Overview landing and a light "product" redesign — the whole dashboard now matches the projectmem brand, and the new first screen shows all four lenses at a glance.** Opening `pjm visualize` used to drop you straight into the Story Map force graph and make you tab around to assemble a mental picture. 0.1.4 adds an **Overview** tab (now the default) that puts the four lenses — failure heatmap, ROI, project structure, and timeline — in a single 2×2 glance, and re-themes the entire dashboard from the old dark palette to a clean light theme drawn from the poster/brand colors.

### New: Overview tab (default landing)

The first thing you see is now a calm, all-at-once summary instead of a graph you have to interpret:

- **Story Map → failure heatmap.** The top files ranked by effort burned (failed attempts ×3 + mentions), bar colour deepening from blue to red with failure intensity. The one chart that answers "where is this project bleeding time?" without a single click.
- **ROI Dashboard → headline cards + a prevention-grade gauge.** Tokens saved, debugging hours saved, USD saved, plus a real A+→F semicircle gauge bound to `pjm score` (same single ROI model — no second source of truth). The gauge colour tracks the grade band.
- **Project Map → compact node graph.** A 10-node summary of `PROJECT_MAP.md` with folders larger, and a red dashed ring on any file that has recorded failures — so structure and pain show up together.
- **Timeline → swimlanes.** issue / attempt / fix / decision as four horizontal lanes of dots across the project's real date range, with an auto-scaled month axis.

Each panel has an `open ↗` link that jumps to the full interactive tab.

### Light "product" re-theme across every tab

The dashboard's CSS variables were re-grounded in the projectmem brand palette (navy header, blue/teal/coral accents on a light surface). Story Map, ROI, Project Map (tree + graph), and Timeline were all audited for dark-only colours — invisible-on-light graph labels and the dark legend/tooltip cards are fixed — so the four detail tabs and the new Overview now read as one coherent app. The grade/score for the gauge is injected via a new `{{SCORE_DATA}}` payload built from `calculate_score`. No data-model or CLI-surface changes; `pjm visualize` flags are unchanged.

## 0.1.3

**Six focused improvements: schema enrichment, secret redaction, the conda/venv hook fix (L-047), stack-aware PROJECT_MAP (L-048), MCP config printed at end of init (L-049), and a silent post-commit auto-capture (L-050).** A metadata pass that lifts Glama tool-quality scores from 75% with one B-tool to a projected ~90% all-A; a privacy guardrail that scrubs accidentally-pasted credentials before they hit disk; a regression fix that restores the pre-commit warning for every conda / pyenv / venv user; and two `pjm init` UX additions that remove the two biggest first-run friction points — *"what is this project?"* and *"how do I wire this up?"*.

### `pjm init` now pre-populates PROJECT_MAP.md from your stack (L-048)

Before 0.1.3, `PROJECT_MAP.md` was a `Status: not created yet` placeholder that an AI session had to fill in by re-reading every manifest and folder — exactly the kind of token-burn projectmem is supposed to prevent. `pjm init` now reads `pyproject.toml` / `package.json` / `Cargo.toml` / `go.mod` directly and writes an actual map: project description, stack tags + frameworks + key libraries, main folders, and entry points. The Setup-Mode prompt becomes *refining* the map, not building it from zero. Skip with `--no-stack-detect`. **Safety:** only overwrites when the current map still contains the placeholder marker — a human- or AI-edited map is never clobbered.

### `pjm init` now prints the MCP client config block at the end (L-049)

Every new user used to ask the same question right after `pjm init`: *"OK, what JSON do I paste, and where?"* The README had it, but six clicks deep. Now init ends with a copy-pasteable config block (absolute `sys.executable` baked in to dodge the Claude-Desktop / Cursor PATH-inheritance gotcha) plus the on-disk config-file paths for Claude Desktop, Cursor, Antigravity (legacy IDE), and Codex (TOML reminder included). Skip with `--no-mcp-config`. Notes the Antigravity v2 path may differ — the v1 path is the verified one.

### Post-commit auto-capture no longer prints over the shell prompt (L-050)

The post-commit / post-merge hooks run `pjm _auto-capture` in the background (with `&`). Before 0.1.3 the snippet redirected only stderr (`2>/dev/null`), so the success line — *"[projectmem] Auto-captured: …"* — printed to stdout *after* `git commit` had already returned the prompt to the user. Visually it looked like the terminal was stuck waiting for input; users would press Ctrl-C to "recover" when in fact their keystrokes were already being captured by the shell. Now both streams are redirected (`>/dev/null 2>&1 &`), so the capture stays silent. Verify it ran via `pjm show` if you want to see the event.

### Pre-commit warning silently no-op under conda / venv (L-047, fixed)

### Pre-commit warning silently no-op under conda / venv (L-047, fixed)

The killer feature — `git commit` warning you about repeating a failed approach — was silently broken for the majority of Python users since 0.1.1. The installed hook relied on `command -v pjm` to find the binary at commit time. Git invokes hooks via a non-interactive bash, which does not run `.zshrc` / `.bashrc`, so conda / pyenv / venv PATH modifications were absent and the lookup quietly returned nothing. The hook ran, found no `pjm`, exited 0, and produced no output. Users saw a normal commit and assumed projectmem had no warning to give. In fact it had the warning, ready, in memory — and no way to deliver it.

The fix: `install_hooks` now resolves the absolute path to `pjm` (via `shutil.which`, falling back to `sys.prefix/bin/pjm`) at install time and bakes it into the hook as `PJM_BIN="/abs/path/to/pjm"`. A runtime `command -v` fallback remains for the rare case where the install-time binary was later moved. The hook script's `if [ -d ".projectmem" ] && [ -n "$PJM_BIN" ]` guard means a stale path still degrades gracefully instead of erroring at commit time.

Verified end-to-end with a regression test that simulates git's non-interactive hook environment (stripped PATH, no shell init), and with a real `git commit` in a conda-installed setup — the warning now appears reliably. Users who installed projectmem before 0.1.3 need to run `pjm hooks install` once after upgrading to refresh the baked path.

### Secret redaction on write (new)

projectmem stores event text verbatim in `.projectmem/events.jsonl` — that's the local-first promise. The flip side is that a careless paste ("the bug repros when I set `OPENAI_API_KEY=sk-...`") used to land that key on disk in plain text, often in a file that's then committed to git. Starting in 0.1.3, `storage.append_event` runs a conservative pattern scrubber across the event's user-supplied text fields (`summary`, `notes`, `command`, `git_message`, `location`) **before** anything touches disk. Matches are replaced with `[REDACTED:<kind>]` and a one-line stderr notice fires so the user knows redaction happened.

Patterns covered: OpenAI / Anthropic / OpenRouter `sk-…` keys, GitHub classic tokens (`ghp_…`, `gho_…`, `ghu_…`, `ghs_…`, `ghr_…`) and fine-grained PATs (`github_pat_…`), AWS access key IDs (`AKIA…`), Google API keys (`AIza…`), Slack tokens (`xox[abprs]-…`), Stripe live/test keys (`sk_live_…`, `pk_live_…`, etc.), JWTs (`eyJ…`), `Bearer` tokens, and PEM private-key block headers. Patterns are intentionally narrow — anchored to recognisable prefixes with minimum-length requirements — so ordinary debugging prose (`"tried contain: layout"`, `"forgot password reset flow"`) is never touched. 29 new tests pin both the true-positive and false-positive behavior.

Default on. Escape hatch: `PROJECTMEM_NO_REDACT=1` skips scrubbing entirely (for debugging the redactor itself or for trusted offline contexts). Redaction is wrapped in a defensive try/except so a scrubber bug never blocks the primary write path — better a logged secret than a lost event in a tool whose job is logging.

### MCP tool schema enrichment

No behavior changes; this gives every tool's parameters real `description` fields in the JSON schema (via Pydantic `Field` annotations through FastMCP), plus a one-line side-effect / read-only callout in each tool's docstring.

The Glama tool-quality evaluator was flagging Parameters at 1-2/5 across the entire surface because parameters had only names and types — agents had to guess what `summary`, `location`, `outcome`, `library`, `tokens`, `focus`, `query`, `limit`, `issue_id`, and `file_path` actually meant. They now have explicit descriptions and, where useful, schema constraints:

- `search_events.limit` now enforces `1 ≤ limit ≤ 100` in the schema.
- `record_attempt.outcome` now enforces the pattern `^(worked|failed|partial)$` — invalid outcomes are rejected at the schema layer, not silently coerced.
- `get_context.tokens` now enforces `100 ≤ tokens ≤ 20000`.

Every tool also gained one short docstring line stating side effects (e.g., *"Appends an `issue` event to `.projectmem/events.jsonl`, creates an issue file in `.projectmem/issues/`, updates `summary.md`, and marks this issue as the active one"*) or its read-only nature. The MCP `instructions=` block was already strong and was left alone.

**What didn't change:** function bodies, return values, defaults, parameter names, parameter order, the 14-tool surface, the CLI, storage layout, hooks, watcher, and the test suite (still 12/12 passing). All existing MCP client configurations continue to work without modification.

Bumped to 0.1.3 to publish the richer schema to both PyPI and the official MCP Registry. Detailed per-tool analysis and rollback plan in [report/GLAMA_QUALITY_IMPROVEMENT_PLAN.md](report/GLAMA_QUALITY_IMPROVEMENT_PLAN.md).

## 0.1.2

**Metadata-only republish to satisfy the official MCP Registry's package-ownership check.** Added the line `mcp-name: io.github.riponcm/projectmem` to `README.md` so the registry can verify the GitHub namespace owner (`riponcm`) controls the published PyPI package. No code or behavior changes.

## 0.1.1

**First stable public release.** v0.0.6 was the intelligence layer; v0.1.1 is the polish + cross-client verification + cross-project memory wiring that makes it ready for general use. 46 lessons logged from soft-launch dogfooding and live verification, a 22-item batch polish-pass + 6 follow-up fixes, plus **end-to-end verification across all 4 major MCP clients** (Antigravity, Claude Desktop, Cursor, Codex) **and across 3 language ecosystems** (JavaScript, Python, Go) for cross-project memory.

This is the version we're comfortable putting on real PyPI. The CLI surface, MCP tool list, event schema, and `.projectmem/` layout are stable from here — future minor versions (0.2.0, etc.) will add features, not break the existing contract.

### Cross-project memory — wiring restored (the big one)

The "Cross-Project Knowledge" diagram on the landing page promised library gotchas to propagate machine-wide. Verification revealed it shipped half-built — three concrete gaps fixed before 0.1.1:

- **Auto-promote never fired on writes (L-043)** — `auto_promote_event` existed in `global_memory.py` but no write path called it. Every `record_attempt` / `add_decision` / `add_note` (MCP and CLI) silently skipped global promotion. Wired into `storage.append_event` so every write surface now promotes consistently. Plus word-boundary library matching (no more "gin" inside "imagineering") + stack-filter (a vite project mentioning "next" in plain English no longer creates a fake Next.js gotcha).
- **Library set was JS/Python-only (L-045)** — the hardcoded `PROMOTABLE_LIBRARIES` set covered React/Vue/Next/Vite/FastAPI/Django and not much else. Go, Rust, Java, Ruby, .NET, mobile — all silently dropped at promotion time. Replaced with a self-curating cache at `~/.projectmem/global/.promotable.json`: every library `detect_stack` ever sees in a manifest on this machine becomes promotable. A Go user's `gin` decisions now propagate exactly like a React user's `vite` ones.
- **Every library mention was treated as a gotcha (L-046)** — `add_decision("Use FastAPI for this project")` used to pollute the global store with project-local setup choices. Now there's an explicit signal filter: failed/partial attempts always promote (the outcome is the signal); decisions/notes only promote when their summary opens with `gotcha:` / `lesson:` / `warning:` / `caution:` / `pitfall:` / `avoid:` / `don't` / `do not` / `never` / `bug:`. Result on the test cycle: signal-to-noise went from 14% to 100%.

End-to-end verification across `globaltest/proj-react`, `proj-next`, `proj-python`, and `proj-go`: a `vite` gotcha logged in proj-react surfaces in proj-next with `source_project` attribution, stays out of proj-python and proj-go's responses, and a `gin` gotcha logged in proj-go promotes correctly under the new library cache + signal filter. Full results in [report/CROSS_PROJECT_TEST_PLAN.md](report/CROSS_PROJECT_TEST_PLAN.md).

### Bug fixes (the launch blockers)

- **MCP stdio integrity (L-009 + L-010)** — write tools used to corrupt the JSON-RPC stream via `typer.echo`, and one bad call would kill the entire session. Every tool body now runs inside a stdout-suppression context + a `@safe_tool` exception wrapper. Five consecutive write-tool calls survive cleanly in any client.
- **MCP project-root discovery (L-005)** — server used to fail with *"No .projectmem directory found"* when the MCP client launched it from its own CWD. New parent-walk fallback (like git does for `.git/`), plus a `--root` flag and `PROJECTMEM_ROOT` env var for explicit pinning.
- **Silent issue misattribution (L-027a)** — `pjm attempt` after a `pjm fix` used to silently attach to whatever issue was still open. Now uses a `.projectmem/.current_issue` marker, a 5-minute time-fence on the fallback, and an explicit `--issue <id>` flag.
- **Partial attempts dropped from summary (L-027b)** — `summary.md` only surfaced `failed` attempts; `partial` outcomes vanished even though they contained valuable signal. Now both render.
- **Project purpose stuck on placeholder (L-037)** — `summary.md`'s Project purpose section never escaped its init placeholder. Now auto-syncs from `PROJECT_MAP.md`'s `## Project purpose` section on every regeneration.

### Behavioral fixes

- **AI workflow alignment (L-028 / L-031 / L-036)** — three surfaces used to tell the AI different things about the session-start trio (MCP `instructions=` field, `CLAUDE.md` bridge, `AI_INSTRUCTIONS.md` template). All three now mirror each other: `get_instructions` → `get_summary` → `get_project_map`, plus the *"never edit `.projectmem/` files directly via filesystem write"* rule.
- **AI_INSTRUCTIONS.md rewrite (L-036)** — was CLI-only and out of sync with MCP. Now lists both MCP tools and CLI commands per trigger, distinguishes Setup Mode vs Maintenance Mode by concrete placeholder phrases (not "files populated"), and gives AI clients an imperative 6-step Setup procedure.
- **`pjm init` writes a CLAUDE.md bridge (L-004f)** — marker-bounded block at project root, idempotent on re-init. AI clients (Claude Code, Antigravity, Cursor) honor the memory layer by default.

### Quality of life

- **ROI surfaces reconciled (L-025d)** — `pjm stats` and `pjm score` used to report different `tokens_saved` numbers. `pjm stats` is now a thin presentation layer over `score.calculate_score` — single source of truth.
- **`pjm score --verbose` works (L-025a)** — was a no-op; now appends per-component event detail so you can audit exactly why the score is what it is.
- **`pjm stats --format json` (L-025b)** — CI-friendly JSON output matching `pjm score`'s format flag.
- **`pjm visualize --output / --no-open` (L-024b)** — choose where the HTML lands, skip auto-open (CI / headless).
- **`pjm search --regex` (L-027c)** — opt-in regex / OR-pattern search.
- **`pjm attempt --issue <id> / --auto-issue`** — explicit issue attribution + auto-creation of an implicit parent issue when none is open.
- **`pjm wrap` File Gotchas filter (L-022a)** — auto-backfill events used to pollute the gotchas section with the same generic note per file. Filtered out.
- **`pjm global` ergonomics (L-026b)** — `pjm global add "..." --library X` auto-routes to `add-gotcha`. Plus `--format json` on `list` / `detect` (L-026c).
- **Framework detection word-boundary fix (L-026a)** — `pjm global detect` no longer flags `gin` (Go framework) when scanning a React project with `eslint-plugin-react` (substring match on `plugin`). Word-boundary regex now.
- **Timestamp normalization (L-024a)** — `pjm visualize`'s Timeline used to show "INVALID DATE" on auto-backfill events. All events normalized to ISO-Zulu on write + defensive parser in the dashboard.
- **HIGH CHURN counter via git log (L-023a)** — was reading from event log (stale); now sources from `git log --since=N.days.ago` (live).

### Cross-client MCP verification

All four major 2026 MCP clients tested end-to-end against a real project:

- **Antigravity** — first client dogfooded; entire v0.0.6 bug list was found here, batch fix landed, every category re-verified.
- **Claude Desktop** — must use **Auto mode** (Plan mode bypasses MCP), pass project root via **`--root` in args** (the `cwd` JSON field is silently ignored in current builds), worktree mode requires init inside the worktree.
- **Cursor** — same `--root` workaround for the cwd-ignored bug. Per-project `.cursor/mcp.json` supported.
- **Codex** — config is **TOML at `~/.codex/config.toml`** (not JSON), UI Save button can silently fail (edit the file directly), set **reasoning effort to `medium` or higher** for the full session-start trio.

### Docs

- **README hero + demo images** now hosted on a separate public asset repo (`github.com/projectmem/projectmemdoc`) and referenced via `raw.githubusercontent.com`. PyPI's README renderer can fetch them regardless of the projectmem repo's visibility. Animated GIF (8-frame, Safari-safe) replaces the SVG that PyPI rendered inconsistently.
- **All 4 MCP clients' UI navigation paths documented** in README + Guide (Settings → Developer → ..., Settings → Tools & MCPs → ..., etc.).
- **First-run permission prompts callout** — documented as normal MCP-client behavior, not a bug.
- **Stale MCP server process troubleshooting** — common gotcha when iterating on MCP config; documented diagnostic commands + recovery.

### Carried over to v0.0.8 backlog

- L-038 (`pjm watch` duplicate churn events for one incident) — known cosmetic noise; documented fix queued for v0.0.8 polish.
- Universal AI Bridge (`pjm bridge install`, `pjm doctor`) — multi-bridge `pjm init` writing `.cursor/rules/`, `.github/copilot-instructions.md`, `AGENTS.md`, etc.

---

## 0.0.6

projectmem transforms from a passive memory logger into an active intelligence layer. Zero-friction capture, intelligent injection, provable ROI, cross-project knowledge.

### Major Features

- **Auto-Capture Engine** — git hooks now classify commits into the right event types automatically. `revert` becomes a failed attempt, `fix:` becomes a fix event, `feat:` becomes a note, `BREAKING` becomes a decision. Zero manual logging required for common cases.
- **Pre-Commit Warnings (`pjm precheck`)** — the killer feature: warns you BEFORE you commit if you're about to repeat a failed approach, modify a high-churn file, or touch an unresolved issue. No other AI tool can do this — it requires the memory layer underneath.
- **Smart Context Injection (`pjm wrap`)** — wraps your AI agent (Claude, Cursor, Aider) and auto-injects a token-budgeted context block before the session starts. Inject into `CLAUDE.md`, `.cursorrules`, or clipboard.
- **Failure Prevention Score (`pjm score`)** — quantifiable ROI metric with letter grade (A+ through F). Tracks failed approaches on record, decisions documented, debugging hours saved, tokens saved, USD saved. Outputs as terminal display, JSON for CI, or shields.io badge for README.
- **Context Budget Optimizer (`pjm context`)** — generate token-budgeted project memory tailored to file focus and time window. Four compression levels (full / compressed / ultra / emergency). Git-aware — boosts events for files currently being worked on.
- **Cross-Project Global Memory (`pjm global`)** — knowledge that follows the developer across projects. Stores patterns, library gotchas, and stack preferences in `~/.projectmem/global/`. Auto-detects stack on `pjm init` (Python, JS, Rust, Go, Java) and inherits relevant gotchas. Export/import for team sharing.

### Enhancements

- **Auto-installed hooks on `pjm init`** — git hooks are installed automatically. No need to remember `pjm hooks install`. Opt out with `--no-hooks`.
- **Three hooks now installed**: `post-commit`, `post-merge`, and `pre-commit` (for `pjm precheck`).
- **Safe hook installation** — appends to existing hooks with clearly-marked snippets. Never overwrites. Clean uninstall removes only projectmem's section.
- **Visualization overhaul for auto-capture**:
  - New header stat: auto-captured event count
  - `AUTO` badge on auto-captured events in Timeline
  - New ROI cards: Manual / Auto-captured / Would Be Lost / Auto-capture Rate
  - New Capture Sources donut chart (git commits vs reverts vs manual)
  - New File Churn heatmap (top 10 files by activity, color-coded severity)
  - Manual/Auto filter pills in Timeline
  - Dashed borders + transparency on auto-captured nodes in Story Map
- **Event model extension**: `auto_captured`, `capture_source`, `capture_confidence`, `git_message` (backward compatible).
- **AI_INSTRUCTIONS.md template updated** with auto-capture awareness section telling AI agents what's auto-captured vs what still needs manual logging.
- **Hidden `_auto-capture` command** for internal use by git hooks.

### Real-Time File Watcher (`pjm watch`)

- **New command:** `pjm watch [--daemon|--stop|--status]` — opt-in real-time file watcher that detects high churn (4+ edits to the same file within 10 min) and auto-logs them as churn-detector events.
- **Auto-starts on `pjm init`** in interactive terminals — zero-touch experience. Skipped in CI/CD, piped output, and non-TTY environments to avoid zombie daemons.
- **Battery-aware:** idles when no activity, gitignore-aware, single-instance lock via PID file at `.projectmem/watch.pid`, graceful SIGTERM shutdown.
- **Project Map tree view:** new horizontal dendrogram (D3 cluster + bezier links) with zoom/pan, toggleable against the existing force-graph view.
- **Opt-out flag:** `pjm init --no-watch` for power users or battery-conscious environments.

### Zero-Touch Setup

- **Auto-backfill on `pjm init`** — automatically ingests the last 20 git commits as classified events. Fresh repos = silent no-op. Existing repos = instant dashboard with real data. Opt out with `pjm init --no-backfill`.
- **Auto-installed git hooks** + **auto-started watcher** + **auto-backfilled history** + **auto-inherited global memory** all happen in a single `pjm init` call. From `pip install` to active memory in two commands.

### MCP Server Expansion (8 → 14 tools)

Native MCP server now exposes intelligence-layer capabilities to AI agents, not just raw memory:

- **`precheck_file(path)`** — AI can self-check a file's failure history *before* proposing changes (turns memory into proactive judgment).
- **`get_issue(id)`** — lazy-load one specific issue file for token efficiency.
- **`search_events(query, limit)`** — plain-text search over the event log instead of loading the full summary.
- **`get_score()`** — AI can report the prevention score with hours/tokens/dollars saved.
- **`get_context(tokens, focus)`** — AI requests an on-demand token-budgeted context block.
- **`get_global_gotchas(library)`** — AI queries cross-project memory for library-specific lessons.

Existing 8 tools (`get_summary`, `log_issue`, `record_attempt`, `record_fix`, `add_decision`, `add_note`, `get_instructions`, `get_project_map`) unchanged.

### Privacy & Security

- **`SECURITY.md`** at repo root with vulnerability disclosure policy and threat model.
- **Privacy & Security section** in the user guide explaining the team-memory-via-git pattern, local-first guarantees, prompt-injection considerations, and uninstall path.
- **Cleaner gitignore default** — only `events.jsonl`, `watch.pid`, `watch.log` are ignored by default, allowing `summary.md` / `PROJECT_MAP.md` / `AI_INSTRUCTIONS.md` to be shared via git. Opt into total privacy by adding `.projectmem/` to `.gitignore`.

### Dependencies

- `watchdog>=4.0` promoted from optional to required dependency — required for the auto-started file watcher. Adds ~70KB to install size.

### Breaking Changes

None — v0.0.6 is purely additive. Existing `events.jsonl` files continue to work without modification.

## 0.0.4

- **Major Feature**: Complete overhaul of `viz.html` into a stunning, single-page Tabbed Dashboard (Story Map, ROI Dashboard, Project Map, Timeline).
- **Major Feature**: Automated D3.js Architecture Graph generation—`pjm visualize` now natively parses your Markdown `PROJECT_MAP.md` into an interactive node graph with zero extra AI tokens.
- **Enhancement**: Upgraded dashboard aesthetic to a high-end, soothing "Midnight Blue & Indigo" professional palette to reduce developer eye strain.
- **Enhancement**: Added explicit documentation and guarantees in the README for configuring native MCP vs Custom System Prompts for 100% hands-free workflows.

## 0.0.3

- **Major Feature**: Native MCP Server (`pjm-mcp`) for direct integration with Claude Desktop and Cursor.
- **Major Feature**: Interactive D3.js visualization (`pjm visualize`) showing project story and technical debt heatmap.
- **Major Feature**: Auto-backfill (`pjm backfill`) to ingest git history into project memory.
- **Major Feature**: Token ROI Dashboard (`pjm stats`) to calculate and visualize AI tokens saved.
- **Major Feature**: 3-Level Auto-Tracking system for hands-free memory management:
  - Level 1: Trigger-based `AI_INSTRUCTIONS.md` with MANDATORY rules that force AI agents to log work automatically.
  - Level 2: MCP server with built-in system prompt and proactive tool descriptions (MANDATORY/IMMEDIATELY language).
  - Level 3: Auto-capture Git Hooks that log `revert`, `fix:`, `feat:`, and `BREAKING` commits passively.
- **Enhancement**: Added `pjm` alias globally to prevent conflicts with other system tools.
- **Enhancement**: Added location metadata (`--at`) support to all logging commands.
- **Enhancement**: Added `get_instructions()` MCP tool so AI agents can read project rules natively.
- **Enhancement**: Added "Maintenance Mode" logic to `AI_INSTRUCTIONS.md` to prevent redundant structural mapping.
- **Fix**: Corrected JavaScript syntax error in D3.js forceLink chain that prevented visualization rendering.

## 0.0.2

- Add `.projectmem/AI_INSTRUCTIONS.md` during initialization.
- Add `.projectmem/PROJECT_MAP.md` as the AI-created structural map placeholder.
- Add `pm instructions` to print the project AI memory protocol.
- Add `pm map` to print the project map.
- Improve the initial `summary.md` so new projects are not blank.

## 0.0.1

- Initial local MVP scaffold.
