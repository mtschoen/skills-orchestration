# fleet-orchestration

A skill for dispatching subagents across **multiple local project repositories** - feature implementation, maintenance sweeps, or fleet-wide investigation. Extends `superpowers:dispatching-parallel-agents` with the cross-repo governance that skill's single-codebase assumption doesn't cover.

## When it fires

Multi-repo subagent work: "spawn agents to fix X across all my projects", "run a maintenance pass", "work on tasks from several repos at once", or any use of project-tracker CLI commands (`list`, `find-dirty`, `find-stale-maintenance`) to plan multi-repo work.

For fixing several bugs in **one** repo, use the parent skill alone.

## What it does

Adds task selection/triage across the fleet, a user pre-approval gate (feature mode), result triage (the orchestrator answers agents' open questions rather than forwarding them verbatim), the maintenance-vs-feature dual mode, `.maintenance.json` breadcrumbs, cross-repo permission and worktree-isolation guidance, and the persistent warm worktree pool protocol for projects whose first build is expensive.

The authoritative spec is [`SKILL.md`](SKILL.md).

**Repo:** <https://github.com/mtschoen/skills-fleet-orchestration>
