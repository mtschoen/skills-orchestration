# `.maintenance.json` Format Reference

Repo-local breadcrumb file recording the last run of recurring maintenance tasks. Lives at the root of each project repo, **tracked** (not gitignored): it records which findings were actioned and which were deliberately rejected, and nothing regenerates that history. The machine-wide excludes file ignores `.maintenance.json`, so a compliant repo's `.gitignore` carries an explicit `!.maintenance.json` negation to override it.

## Schema

```json
{
  "version": 1,
  "tasks": {
    "<task-name>": {
      "kind": "per-commit" | "time-based",
      "last_run_commit": "<full-sha>",   // per-commit only
      "last_run": "<iso-8601 utc>",       // both kinds
      "interval_days": 30,                 // time-based only
      "status": "clean" | "failed",        // optional, informational
      "last_error": "..."                  // optional, informational
    }
  }
}
```

## Field rules

- `version`: integer schema version. Currently `1`. Bump on breaking changes.
- `kind`: required. Determines staleness logic.
- `last_run_commit`: full 40-char SHA, not abbreviated. Required for `per-commit`.
- `last_run`: ISO 8601 UTC timestamp. Required for `time-based`, optional but recommended for `per-commit`.
- `interval_days`: number. Required for `time-based`. Per-task, so different repos can have different cadences.
- `status`: not read by the staleness check, but written by runners for human inspection and future use.
- `last_error`: optional short error string. Same caveat - informational only.

## Staleness rules

| kind | Stale when |
|---|---|
| `per-commit` | `last_run_commit != git rev-parse HEAD` |
| `time-based` | `now - last_run > interval_days` |
| missing entry | always (first run) |
| unknown `kind` | always (defensive) |

**Critical rule for failures:** a task with `status: "failed"` at the same HEAD is **not** stale. Same commit → same failure. Wait for HEAD to move before retrying. This applies automatically because the staleness check only compares `last_run_commit`, regardless of `status`.

## Standard task names

Reserved names - use these exactly so project-tracker and the orchestrator skill can reason about them across repos:

| Name | Kind | Purpose |
|---|---|---|
| `push-latest` | per-commit | Push current branch to upstream if behind |
| `lint-changed` | per-commit | Run linter/formatter on changed files since last clean |
| `tests-pass` | per-commit | Run test suite, mark clean if green |
| `tick-completed-tasks` | per-commit | Tick PLAN.md items completed by recent commits |
| `stale-worktrees` | time-based | Prune merged git worktrees |
| `gone-branches` | time-based | Delete `[gone]` branches |
| `dependency-freshness` | time-based | Check for outdated dependencies |
| `memory-prune` | time-based | Prune stale auto-memory entries (orchestrator-side, not per-repo) |

Add new names freely; just be consistent across the fleet.

## Example

```json
{
  "version": 1,
  "tasks": {
    "push-latest": {
      "kind": "per-commit",
      "last_run_commit": "190f77f8e9c1b4d2a5f0e3b8c7a9d1e2f3b4c5a6",
      "last_run": "2026-04-06T15:42:00Z",
      "status": "clean"
    },
    "stale-worktrees": {
      "kind": "time-based",
      "last_run": "2026-04-01T03:00:00Z",
      "interval_days": 30,
      "status": "clean"
    },
    "lint-changed": {
      "kind": "per-commit",
      "last_run_commit": "cded4b8c0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d",
      "last_run": "2026-04-05T22:11:00Z",
      "status": "failed",
      "last_error": "ruff: 3 errors in src/foo.py"
    }
  }
}
```

In this example:

- `push-latest` is clean at the current HEAD → skip on next pass.
- `stale-worktrees` is clean and well within its 30-day window → skip.
- `lint-changed` failed at commit `cded4b8`. If HEAD is still `cded4b8`, **skip** (it'll fail again). When HEAD moves, retry.

## project-tracker integration

project-tracker reads these files via its maintenance scanner module and exposes two CLI queries:

- `project-tracker maintenance state <name> --json` -> parsed contents
- `project-tracker find-stale-maintenance --task <task_name> --json` -> stale projects

The Python helpers (importable from the maintenance scanner module):

- `read_maintenance_state(project_path) -> dict`
- `write_maintenance_state(project_path, state)` - writes the JSON, then touches `.gitignore`
- `is_task_stale(project_path, task_name, *, state=None, head=None, now=None) -> bool`
- `list_stale_tasks(project_path, task_names=None) -> list[str]`

**Known defect in `write_maintenance_state`:** as of the currently installed project-tracker release, its `.gitignore` handling writes a bare `.maintenance.json` line - a positive ignore rule - instead of the `!.maintenance.json` negation this tracked-file contract requires. Calling it therefore leaves `.maintenance.json` gitignored and absent from `git status`, the opposite of the intended behavior. Runners should still call `write_maintenance_state` for the JSON write (it handles serialization and the `version` default correctly), but must check `.gitignore` afterward: if it contains a bare `.maintenance.json` line, replace it with `!.maintenance.json` and `git add .maintenance.json .gitignore`. Both the Python helpers and CLI require project-tracker to be installed - it's optional (see SKILL.md). Without it, write the JSON entry directly per the schema above and add the `!.maintenance.json` negation to `.gitignore` yourself; the staleness rules are unaffected either way, since they only depend on the file's contents.
