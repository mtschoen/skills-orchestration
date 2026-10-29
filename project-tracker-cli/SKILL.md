---
name: project-tracker-cli
description: Use when querying or changing tracked projects, tasks, plans, maintenance records, goals, or fleet state through the project-tracker command line.
---

# project-tracker CLI

Run commands such as `project-tracker list --json` and read the structured
result. Use the command's `--help` option for exact arguments. The groups below
cover the agent-facing operations.

## Commands by task

| Task | Commands |
| --- | --- |
| Find projects and work | `list`, `show`, `find-open-tasks`, `find-dirty`, `find-unpushed`, `find-stale`, `find-by-tech`, `find-master-branch`, `find-unmerged-branches`, `find-untracked-plan-steps` |
| Inspect projects | `stats`, `github-info`, `issues`, `find-with-issues`, `ship-board`, `reconciliation`, `recent-completions`, `project locations`, `gitwizard-status` |
| Create and classify projects | `status`, `classification`, `ignored`, `favorite`, `kinds`, `people`, `project create`, `project register`, `project merge`, `project link`, `project unlink` |
| Work with tasks | `task add`, `task toggle`, `task hold`, `task plan`, `task due`, `task complete`, `task cull` |
| Work with plans | `plan hierarchy`, `plan designate`, `plan undesignate`, `plan create` |
| Maintain repositories | `maintenance state`, `maintenance checklist`, `maintenance record`, `find-stale-maintenance`, `find-stale-memory` |
| Track sessions and goals | `list-wip`, `record-wip`, `clear-wip`, `goals`, `goals brief`, `goals brief store` |
| Inspect fleet state | `fleet status`, `fleet list`, `fleet unsynced` |

Useful starting points:

```bash
project-tracker find-open-tasks --max-per-project 10 --json
project-tracker reconciliation example --max-cache-age-seconds 0 --json
project-tracker maintenance checklist example --json
project-tracker task toggle example PLAN.md 8 --expected-title "Task text" --json
```

## JSON and mutations

Every command above supports `--json`. Mutating commands that advertise
`--stdin-json` in their help read one JSON object with Python argument names such
as `name`, `source_file`, `line_number`, `expected_title`, and `summary`. Required
arguments may come entirely from stdin. Do not supply the same key on the command
line and in stdin. JSON booleans and numbers retain their types.

```bash
project-tracker maintenance record --stdin-json --json <<'JSON'
{"name":"example","summary":{"timestamp":"2026-10-28T00:00:00Z","head":"abc123","automated":[],"user_authorized":[],"rejected":[]}}
JSON
```

A failed operation returns an error object and exits 1. Invalid arguments exit 2
before mutation. A timeout can abandon the client thread while the underlying
write continues, so inspect state before retrying.

## Result bounds and people

JSON result shapes vary by command family. Check each command's actual `--json`
output (and `--help` for `--limit`) rather than assuming a universal envelope:

- **Bounded commands** (commands that accept `--limit`: `find-with-issues`,
  `find-unmerged-branches`, `recent-completions`, `fleet unsynced`, `issues`,
  `reconciliation`, `plan hierarchy`, `goals`, and `goals brief`) cap
  collections and report truncation. List-returning bounded commands (for
  example `project-tracker find-with-issues --json`) return
  `{"items": [...], "truncated": false}`; object-returning bounded commands keep
  their metadata, cap nested collections, and include `truncated` (`goals brief`
  nests this under `snapshot`). `truncated: true` means entries were omitted;
  increase `--limit` when needed rather than assuming a partial result is
  complete.
- **Project list** (`project-tracker list --json`) returns
  `{"projects": [...], "warnings": [...]}` with no `truncated` field.
- **Legacy list queries** without `--limit` (such as
  `project-tracker find-open-tasks --json` and
  `project-tracker find-dirty --json`) return a bare JSON array `[...]` with no
  `truncated` field.

`PROJECT_TRACKER_FOR_PERSON` selects the default household view only. It does not
set attribution. Use `--for-person` to override the view and `--actor` to name who
performed a mutation. An omitted actor falls back to the configured
`default_actor`; it does not inherit `PROJECT_TRACKER_FOR_PERSON`.

`task hold` and `task plan` use `--expected-title` to reject writes when the line
changed. `task toggle` and `task complete` also accept `--actor`; use the raw task
title from the returned row, not a paraphrase.
