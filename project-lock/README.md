# skills-project-lock

Cooperative project and Git-worktree locks for autonomous coding agents.

The repository contains a skill plus a dependency-free Python CLI. Agents check and acquire a lock before writing outside or inside their starting directory. Lock metadata tells contenders who is working, why, when they expect to finish, and whether waiting or a separate worktree is preferred.

## Quick start

```bash
python scripts/project-lock.py check .
python scripts/project-lock.py acquire . --reason "main session" --duration 2h --strategy worktree
python scripts/project-lock.py list
python scripts/project-lock.py watch
python scripts/project-lock.py release . --lock-id <id>
```

A lock is the atomic directory `<root>/.agent-lock/`, with versioned metadata in `owner.json`. Git's local exclude file hides the marker from `git status`. Per-user SQLite serialization makes acquire, renew, and release generation-safe, and a per-user registry powers the watcher.

Elapsed estimates become **overdue**, never automatically free. Clear an abandoned lock only after verifying its owner is gone:

```bash
python scripts/project-lock.py release /path/to/project --force \
  --expect-lock-id <the id you verified> --reason "owner session exited"
```

`--reason` is recorded and `--expect-lock-id` is a compare-and-swap, so an override that races a replacement lock fails instead of discarding it. Every force-clear is appended to `audit.jsonl` in the per-user state directory *before* the lock is removed, making "no unaudited force-clear" an invariant rather than a best effort. `check` reports the owner process as `gone`, `running`, `running-unverified`, or `unknown`, comparing the live process against a start identity recorded at acquire time so a reused pid reads as `gone` rather than as a live owner. That is evidence for a human decision, never an automatic verdict.

Liveness is opt-in via `acquire --owner-pid`, and defaults to `unknown`. `acquire` reads `--session` and `--owner-pid` from a recognized harness environment variable (Claude Code sets `CLAUDE_CODE_SESSION_ID` and `CLAUDE_PID`) when the flag is omitted; an explicit flag always wins. Nominate only a process that outlives the command, such as the agent session: the `acquire` process itself exits as soon as it writes the marker, so tracking it would report `gone` on every healthy lock.

## Decisions

Codex's `CODEX_THREAD_ID` is the first automatic session candidate, ahead of an
inherited Claude or Pi identity. Codex does not export a supported durable owner
PID, so automatic liveness remains unknown instead of borrowing `CLAUDE_PID`.
Explicit `--session` and `--owner-pid` always win; use them for other mixed-harness
nesting where inherited identity variables could be ambiguous.

The shell guard recognizes plain PowerShell read cmdlets as read-only. Expressions,
script blocks and writes in the same command stay conservatively classified as
mutating; this remains a cooperative heuristic rather than a shell security boundary.

- Wait for short work, same-branch changes, maintenance, merges, rebases, and shared mutable state.
- Use a separate Git worktree for independent long-running edits.
- Lock the new worktree before editing it.
- Use a real transactional lease service for cross-host or high-impact external operations.

## Enforcement hook

`hooks/pre_tool_use.py` is a mechanical `PreToolUse` hook. Wire it in the user-level agent settings.

For example on Claude Code, use any `settings.json` (portable form, no machine paths):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write|NotebookEdit|Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python \"$HOME/.claude/skills/project-lock/hooks/pre_tool_use.py\""
          }
        ]
      }
    ]
  }
}
```

Windows settings use `%USERPROFILE%` semantics: expand via `$env:USERPROFILE` or write an absolute per-machine path. Settings are per-machine config, not a shipped file, so this snippet is a template to adapt, not something the installer writes for you.

The hook denies `Edit`/`Write`/`NotebookEdit` calls whose target is governed by another session's lock: Git checkout or not, any registered `.agent-lock/` marker above the target governs it. When nothing is registered, the hook denies with the exact acquire command (including `--session` when the payload carries one), including in non-Git project directories, since agents still need to coordinate writes there. The only exception is true scratch space: a target with no `.git` anywhere above it (`has_git_ancestor()`) *and* under a recognized system temp location (`is_under_temp_dir()`) is always allowed, since there is no project to coordinate. On Windows, "recognized system temp location" means `tempfile.gettempdir()` and `$TMPDIR` only; the `/tmp`, `/private/tmp`, and `/var/folders` literals are POSIX mount paths and are not probed there, since they resolve as drive-relative on Windows rather than denoting "no recognized temp location". A Git checkout under a temp mount (e.g. a test fixture under `/tmp`) still has its own `.git` and is enforced normally. For `Bash`, the command is split into segments on `;`, `&`, `|`, and newlines, and each segment is classified independently; the call is denied only when the session's cwd sits in a foreign jurisdiction and the command is not read-only, or when a non-read-only command spells out an absolute path (including `~`- or `$VAR`-prefixed forms, expanded before comparison) under a registered foreign lock. Read-only recognition covers a fixed allowlist (`ls`, `cat`, `head`, `tail`, `grep`, `rg`, `pwd`, `echo`, `which`, `type`), `find` without a mutating flag (`-delete`, `-exec`, `-execdir`, `-ok`, `-okdir`, `-fprint*`, `-fls`), and `git status`/`log`/`diff`/`show`/`ls-files`/`rev-parse`. `git branch` and `git remote` are deliberately treated as non-read-only across the board: their mutating forms are indistinguishable from their read forms to a word-level classifier, so the safe default is to require a lock. Any segment containing `>`, `$(`, a backtick, `<(`, or `--output` is treated as a write regardless of the leading command. Enforcement modes: `PROJECT_LOCK_ENFORCE=deny` (default), `warn` (logs but allows), `off`. The hook fails open on its own errors so a bug in the check never bricks the harness.

Git administration state is shared by every worktree, so it answers to the *governing checkout* - the one whose `.git` the path belongs to, normally the main worktree - rather than to whichever lock happens to sit above the path. A direct file-tool write is allowed when the session holds that checkout's lock, or when nothing anywhere in the repository is locked; it is denied when another session holds that checkout, or when it is unlocked while some other worktree of the repository is locked. Holding the main checkout therefore means being in charge of the repository, and claiming it is the only way in - there is no separate override, which keeps the authority visible to other agents. This matters because Git has no command for some legitimate edits: `.git/info/exclude` and `.git/hooks/*` have no porcelain equivalent, and a `config` too malformed to parse cannot be repaired with `git config`.

The check covers the paths reported by `git rev-parse --git-dir` and `--git-common-dir` plus the `.git` marker at a worktree root, and short-circuits unless a path component ends in `.git` so ordinary edits pay no subprocess cost. Bash is excluded on purpose: Git writes to `.git` on nearly every invocation.

Unlocked Bash is allowed by design: acquiring a lock itself requires running a command, so the unlocked-write deny only applies to the file tools (`Edit`/`Write`/`NotebookEdit`). Bash enforcement engages only against foreign jurisdictions, never against the mere absence of a lock.

This is bounded coverage, not a sandbox: relative or quoted paths that reach a foreign jurisdiction from outside it are not caught. Absolute paths containing spaces also escape the token scan, since a quoted space is indistinguishable from a token boundary. Hold the lock for every jurisdiction a command spans, especially recursive operations (`rm -rf`, `git clean`, formatter sweeps, `git add -A`).

Known limitations of the scratch carve-out: a stray `.git` directory above a temp root (e.g. an accidentally created empty `~/.git`) makes `has_git_ancestor()` true for the whole temp tree beneath it, disabling the carve-out for everything under it. A symlink inside a real repo that resolves into a temp root is treated as scratch by `is_under_temp_dir()`, since it resolves the target path before comparing. Both are inherent to path-based cooperative coordination, not a security boundary.

When wiring this hook globally (every session on the machine), start with `PROJECT_LOCK_ENFORCE=warn` for the first day, since the deny-when-unlocked behavior applies to every `Edit`/`Write` call on the machine, not only ones inside a project that uses `project-lock`.

## Development

```bash
python -m pytest -q
ruff check scripts tests hooks
ruff format --check scripts tests hooks
```

The runtime uses only the Python standard library.

## License

MIT
