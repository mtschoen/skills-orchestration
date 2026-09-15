---
name: fleet-orchestration
description: "Use when dispatching subagents across multiple LOCAL PROJECT REPOSITORIES - feature implementation, maintenance sweeps, or fleet-wide investigation across the user's project directory. Triggers: 'spawn agents to fix X across all my projects', 'run a maintenance pass', 'work on tasks from several repos at once', reserving or releasing a slot in a persistent warm worktree pool on a project whose first build is expensive (a large node_modules, a Rust target/, a populated ccache, a game engine's asset database), use of project-tracker MCP tools (list_projects, find_dirty, find_stale_maintenance) to plan multi-repo work. Extends superpowers:dispatching-parallel-agents with cross-repo governance. project-tracker MCP tools are optional - without them, use the project-tracker CLI (project-tracker list --json). Fleet-wide enumeration requires project-tracker installed; without it, the user names the target repositories explicitly. Requires the superpowers plugin (dispatching-parallel-agents)."
---

# Fleet Orchestration

## Relationship to `superpowers:dispatching-parallel-agents`

**Builds on** `superpowers:dispatching-parallel-agents`. That skill covers the universal mechanics: when to dispatch (independent domains), agent prompt structure, common mistakes, verification. **Read it first; this skill assumes you know it.**

This skill adds the layer that's specific to working **across multiple repositories owned by the same user** - where the parent skill's "one codebase" assumption breaks down and new failure modes appear:

| Concern | Parent skill | This skill |
|---|---|---|
| When to parallelize | ✅ | inherits |
| Agent prompt structure | ✅ | inherits, adds repo-specific must-includes |
| One codebase, multiple bugs | ✅ | - |
| Multiple repos, one task each | - | ✅ |
| Pre-dispatch task selection / triage | - | ✅ |
| User pre-approval shortlist | - | ✅ |
| Result triage (orchestrator answers questions) | - | ✅ |
| Maintenance vs feature dual mode | - | ✅ |
| Maintenance breadcrumbs (`.maintenance.json`) | - | ✅ |
| Cross-repo permission inheritance | - | ✅ |
| Worktree isolation is intra-repo only | - | ✅ |

If you're fixing 3 unrelated test failures in **one** project, use the parent skill alone. If you're picking tasks from PLAN.md files across **N** projects, use both.

## Requirements

This skill has a hard dependency on the superpowers plugin's [`superpowers:dispatching-parallel-agents`](https://github.com/mtschoen/superpowers) skill - see "Relationship" above. Install superpowers before using this skill; there is no standalone fallback.

Fleet-*wide* enumeration additionally requires [project-tracker](https://github.com/mtschoen/schoen-lab) to be installed, reached through either its MCP server or its CLI. Something has to know which repositories are yours, and that is the only thing on the machine that does. Without it, the user names the target repositories and you work from that list. Everything else in this skill - dispatch discipline, triage, base-commit correctness, worktree isolation - carries no such dependency and works anywhere.

These skills are designed against the superpowers fork at <https://github.com/mtschoen/superpowers>, which changes upstream's rules around parallel subagent dispatch and plan/spec file handling. Notably, official superpowers 6.2.0 forbids dispatching implementation subagents in parallel; the fork's subagent-driven-development adds Parallel Dispatch (Worktree Isolation). Skills that describe parallel SDD assume the fork.

## Overview

You are the orchestrator. The parent skill tells you how to brief and dispatch agents. This skill tells you how to **select** which work goes to which agent across the fleet, **gate** the user before dispatching, and **triage** results before they reach the user.

The single biggest fleet-orchestration failure mode is dispatching a vague task and then forwarding the agent's open questions to the user verbatim. **You** are responsible for answering open questions - by reading more code, by checking git history, by understanding the data model - before bothering the user. The parent skill's "review and integrate" step is necessary but not sufficient: across repos, you also need to *answer*, not just *summarize*.

## Two modes: never mix them

**Maintenance pass**: bounded, mechanical, no product judgment.

- Push latest branch tip if behind
- Lint changed files
- Prune merged worktrees, `[gone]` branches
- Tick PLAN.md tasks completed by recent commits
- Run tests on current HEAD

**Feature pass**: PLAN-driven, may require interpretation.

- Implement an unchecked PLAN.md item
- Fix a reported bug
- Refactor a module

A feature pass needs the triage gate and user pre-approval below. A maintenance pass needs neither - definitionally bounded tasks just run, breadcrumbs prevent re-runs. **A maintenance task that suddenly needs a product call is a leaky abstraction**: stop, demote it to a feature task, and run it through the feature gate.

## Pre-dispatch triage (feature mode)

For each candidate task, before spawning anything, answer four questions:

1. **Is the scope crisp?** Does the PLAN bullet name a specific file/feature, or is it an aspiration ("improve UX")?
2. **Are there hidden coupling questions?** Spend 2–3 minutes reading the surrounding code. If a single field/function turns out to be load-bearing across many files, the task is not bounded. (Real example: a "filename" column that turns out to be a lookup key in five other modules - looks like a label, isn't.)
3. **Would a human need to make a product call mid-implementation?** "Should rename touch disk?" "Which interpretation of this note?" If yes, no agent should be touching it.
4. **Is the test surface trustworthy?** Tests-pass ≠ correct-behavior when the task is about semantics rather than mechanics.

### Decision

- **Green** (all four pass) → eligible for dispatch.
- **Yellow** (one concern) → eligible, but the agent's brief must include "stop and report if you hit X."
- **Red** (two+ concerns, or product call required) → **drop from the dispatch list**. Produce a handoff prompt for the user (see below).

Triage runs **before** the user pre-approval step - the user shouldn't have to evaluate red tasks at all.

### Red flag handoff format

When refusing to dispatch, give the user a paste-ready way to start a new session:

```text
This task needs supervised work in a fresh session. Try:

  cd <absolute path to repo>
  <start a fresh agent session in your harness>

Then paste:

> Working in <project>. Task: <PLAN line, verbatim>.
> Before implementing, investigate: <specific files / functions / db columns>.
> The ambiguity is: <concrete question>.
> Ask the user before writing code.
```

This gets the work done with proper context, without burning your orchestrator turn on something it can't safely do.

## User pre-approval (feature pass only)

After triage, **before dispatching**, present the shortlist to the user and wait for approval. Triage filters tasks the orchestrator *knows* are bad; user approval catches tasks the orchestrator *doesn't know* are bad - usually because the user has context (recent decisions, in-flight refactors, "that area is weedy") that isn't in the codebase or git history.

Format the shortlist as a compact table:

```text
About to dispatch N agents in parallel. Approve?

  # | Project    | Task                                | Risk
  --|------------|-------------------------------------|------
  1 | webapp     | Search/filter (PLAN.md:55)          | green
  2 | myrepo     | Edit model filename (PLAN.md:676)   | yellow
  3 | game-proto | Graceful shutdown (PLAN.md:48)      | green

Yellow notes:
  2: "filename" looks like a label, haven't verified it isn't
     load-bearing. Agent will stop and report if it hits coupling.

Reply: "go", "skip N", "all but N", or ask about any task.
```

Required elements:

- **PLAN.md line numbers** so the user can jump to source.
- **Risk color** (green/yellow - reds are already filtered out) so attention goes to the right rows.
- **One-line "what worried me"** for every yellow.
- **Shorthand response format** so approval is a 2-second decision.

The act of writing the yellow note is itself a forcing function - if you can't articulate what worried you, you probably haven't read enough code to dispatch responsibly. Go read more.

### When to skip the prompt

- **Maintenance pass**: just go.
- **User explicitly said "just go"** in this session or in AGENTS.md (or the project's agent instructions file).

Feature passes always prompt, even when everything looks green.

## Briefing additions for fleet work

The parent skill's prompt structure applies. On top of it, every fleet brief must include:

- **Sync before starting**: agent must run `git pull --ff-only` and `git push` (if upstream exists and local is ahead) **before touching code**. Stops divergence between fleet sweeps and prevents work on a stale tree. If pull is non-fast-forward or push is rejected, STOP and report - do not force, rebase, or merge without orchestrator instruction.
- **Absolute repo path** (`C:\Users\user\<project>`) - agents inherit cwd from the orchestrator, not from the task.
- **One-sentence project description**, or "read AGENTS.md in this directory before starting."
- **Verbatim PLAN.md line + line number** they're implementing.
- **Specific file paths you've already identified** as relevant (the triage reading was not wasted - pass it to the agent).
- **Pre-answered ambiguity** - if triage flagged a yellow concern, the brief must say what to do if the agent hits it. Don't make the agent re-derive your judgment.
- **Permission boundary**: "if Edit/Write is denied for files under `<repo path>`, STOP immediately and report it as a permission issue. Do not work around."
- **Hard prohibitions**: "Do NOT commit. Do NOT push. Leave changes in the working tree." No-commit is the fleet default: the orchestrator commits after review. If you instead brief implementers to commit to isolated branches (the review-in-parallel-pipelines pattern), say so explicitly in the brief - either way, nothing merges or pushes without orchestrator review.
- **Concise reporting format**: files changed (with one-line summary each), test results, anything needing human review.
- **Close-out findings section**: the final report must include a distinct "non-obvious durable findings" section - environment gotchas, stale refs, wrong assumptions found in docs/specs, anything the next agent would waste an hour rediscovering. "None" is a valid entry. Subagents do NOT write memory files (parallel writers race, duplicate, and lack curation context); the **orchestrator** curates worthwhile findings into memory after result triage. Without this line in the brief, discoveries die in the transcript - subagent sessions have no /wrap step.

A great fleet brief includes the answer to the ambiguity you'd otherwise have had to chase down later.

## Model routing

- **Orchestrator seat is the premium-tier model** - reserve it for judgment: triage, writing briefs, synthesizing results, and any delicate or irreversible step (live-system changes, destructive git, cross-repo merges).
- **Worker lanes default to the mid-tier model** - not just for search and mechanical work. A well-specified feature implementation, a multi-file refactor, or a test-writing lane all belong on the mid tier when the brief is concrete; premium tier is not the default just because the task is "real work."
- **Tiers are cross-provider.** "Premium" and "mid" are capability tiers, not vendor names - each harness maps a tier to its own concrete model (Opus-class and Sonnet-class equivalents exist at every major provider, including local models). Keep briefs and routing decisions in tier terms; the tier-to-model table and any provider-pool economics (e.g. a provider whose tokens are effectively unmetered for you is the right home for sustained-volume lanes) belong in the harness's rules/memory layer, not in the brief.
- **Never let a lane inherit the orchestrator's model by omission** - pass an explicit model on every dispatch call. Omission silently burns the scarcer/pricier tier on work the mid tier handles fine.
- **Delegate inline grunt work instead of doing it in the orchestrator seat.** Log forensics, fan-out searches, report/XML crunching, and mechanical multi-file edits are worker-lane tasks even mid-triage or mid-result-review - "it's quick, I'll just do it here" is the failure mode, not a shortcut.
- **Escalate a single lane to premium only when triage calls for it** - ambiguous architecture, a product call embedded in the task, or a delicate live-system/git step that can't be pre-answered in the brief (see Pre-dispatch triage above). Escalate the lane, not the fleet.

## Long-running external processes (batch test suites, builds)

Any brief that includes a run longer than one tool call (Unity batch suites, full builds - often 10-40 min on slow hardware) must spell out the wait pattern, or the agent will strand itself:

- **The failure mode**: the agent detaches the process (correct - a foreground call would time out), then STOPS its turn "waiting for the notification." A raw detached OS process is not harness-tracked, so nothing ever re-invokes the agent; its stop surfaces to the orchestrator as `completed` with a non-result ("Still running... pausing"). This is the top-level reflex (the main session genuinely gets task-notifications) pattern-matched one level down, where it's wrong.
- **Brief the fix verbatim**: "wrap launch + wait + result-parse in ONE tracked background Bash (`run_in_background`) that exits only when the run is done - wait on the pid or until the results file exists, and put a hard timeout on the run itself (e.g. `timeout 3600 <cmd>`) so a hang can't strand you. Its exit re-invokes you exactly once. Never detach a process and stop your turn to 'wait'." Chained sub-cap foreground polls are an acceptable fallback.
- **Route the artifacts, not just the process.** Send every log and result file to a gitignored directory inside the lane's own worktree, with a timestamp in the filename. Default tool log paths and fixed filenames put parallel lanes in each other's way, clobber the previous run you still need to compare against, and leave stray files that dirty the repo's `git status` for whoever reviews the diff.
- **Orchestrator backstop**: when a lane still stops with "waiting...", don't trust `completed` status - read the result text. Verify the process state yourself (pgrep, lockfile, log mtime) and arm your own watcher (`while kill -0 <pid>; do sleep 30; done` in a tracked background Bash) so you get re-invoked to nudge or intervene. A log that hasn't grown in an hour with the process still alive is a HUNG run: read the log tail for the culprit (often an async op nothing pumps in batch mode), kill it, clear any shared lock, and send the diagnosis back to the agent - don't wait it out.

**Symptom you missed this**: repeated completion notifications from the same agent whose "result" is a status update, each needing a manual nudge; or a shared test-pool lock held for hours by a run whose log went quiet.

## Verification in lanes (measured 2026-09-15)

Seven repair/salvage lanes made 347 pytest, preflight and aislop calls, every one in the foreground, none in the background; one lane ran the identical 3-minute suite five times in a row only to `grep` a different slice of its output; another ran the whole-package suite 16 times chasing a coverage floor one gap at a time. About half of each lane's wall clock was spent blocked on checks it could have overlapped, and half the checks were re-reads. Every brief that includes tests or gates must therefore say:

- **Background anything over ~30 s against an unchanged snapshot.** `run_in_background` with the output teed to a timestamped file in the lane's scratch dir. While it runs, the lane may read docs, inspect previous logs, or plan next steps, but must NOT edit tested source files or configuration during verification - mutating files mid-run invalidates results and creates mixed-state diagnostics. Never re-run a suite to see more of its output; read the teed file.
- **Iterate on targeted tests, verify whole once.** While fixing: the failing files or `-k` selections (`-n auto` where the package's config allows it). At the end: ONE whole-suite run with coverage and ONE aislop pass against the final, unchanged source snapshot. Cap fix-and-rerun at three rounds, then report (or commit to an isolated branch if briefed) with remaining failures listed under "Known gaps" in the final report; the orchestrator reviews the result before any commit, push, or PR, and the orchestrator or the crew's iterate takes it from there.
- **Let CI run the repo-wide pass when configured.** When the target repository has an equivalent CI workflow or preflight runner configured on push, `--changed-from` / `--all` preflights cost 10 to 25 minutes and CI executes them on push anyway; a lane runs one only when the brief says the change touches `ci/` or `tools/` and the orchestrator wants the answer before push. If the target repo lacks equivalent CI push checks (e.g. local-only repos or arbitrary fleet projects without CI), the brief must require local repo-wide verification before the orchestrator integrates.
- **Split implementer and verifier for big repairs.** The implementer reports (or commits to its isolated branch if briefed) once its targeted tests pass; the orchestrator reviews the diff and dispatches a separate mid-tier verifier lane to run the whole suite, aislop, and gates in one pass in its own worktree on that base. The implementer never blocks on a full run, and the verifier's single pass replaces repeated runs. Nothing merges or pushes without orchestrator review.
- **Coverage floors are not the lane's job to close blind.** A salvage or repair lane stops at 99% with the uncovered lines listed in its report (for the orchestrator to include under "Known gaps" in the PR body after review) rather than spending an hour writing tests for branches it did not write; closing the gap is a bounded follow-up lane or the crew's iterate.

**Symptom you missed this**: a lane report whose tool count is in the hundreds with the same suite command repeated back to back, or a lane that has been "verifying" for an hour with no progress or report.

## Pre-flight: check target repos for parallel sessions

Start with a fleet-wide pass, then verify per repo before dispatching.

**Fleet-wide**: `mcp__project-tracker__list_projects()` to enumerate the repos you can target, and `mcp__project-tracker__find_dirty()` to see which of them already have uncommitted changes before you touch anything. Without the MCP server, enumerate with `project-tracker list --json` (the tracker's registry is a SQLite database under `~/.project_tracker/`). Without project-tracker installed at all, ask the user which repositories to target and work from the list they give you, per Requirements above. Do not substitute a recursive filesystem scan: an unbounded walk is a hazard in its own right, and finding every checkout on the disk still would not tell you which ones the user counts as part of the fleet. Either way, compute dirtiness with `git -C <repo> status --porcelain` per repo.

**Per target repo**, before dispatching into it: if the `project-lock` skill is installed, it is the authoritative check - run project-lock's `check <repo>` command (its SKILL.md documents the script location and exact invocation) and follow its check/acquire/advice protocol (wait, use a worktree, or proceed, per its own recommendation). It replaces guesswork with an actual advisory lock another agent would have taken.

When `project-lock` isn't installed, fall back to these ad hoc heuristics - run these three commands and read the output:

```text
git -C <repo> status
git -C <repo> worktree list
git -C <repo> stash list
```

Signs an active parallel session is using this repo: uncommitted changes you didn't make, worktrees under `.worktrees/` (or any other non-main worktree), or named stashes from another session.

Either way, if a conflict is detected, **pre-bake an isolated worktree from HEAD** for your agent before dispatching:

```text
git -C <repo> worktree add <repo>/.worktrees/orchestrator-<task> -b agent/<task> HEAD
```

Then pass that worktree path to the agent in the brief - and tell the agent to `cd` into it and, if `project-lock` is installed, acquire the lock **in that worktree** (locks are per-worktree-root, so a pre-baked worktree needs its own lock, separate from the main checkout's) before writing. Do NOT dispatch into the main worktree of a repo where another session is active. Even if your agent works on a different branch, the working tree itself is shared on disk; their uncommitted changes and yours collide on the same files.

Pre-baking is cheap (~100ms per repo). When in doubt, do it. Cleanup after merging: `git -C <repo> worktree remove <path>` and `git -C <repo> branch -d agent/<task>` (or `-D` if discarded).

**Symptom you missed this check**: your agent reports an Edit denial on a file that was modified by another agent in a different worktree, OR your post-dispatch `git status` in the target repo shows a mix of files you don't recognize alongside your agent's edits.

## Worktree hygiene + base verification (any worktree fan-out, intra- or cross-repo)

`isolation: "worktree"` does NOT branch from your checked-out HEAD. Verified empirically 2026-07-01 (the created branch's reflog reads `branch: Created from origin/main`): auto-created worktrees branch from **origin/main** - the remote-tracking default branch, which may be stale and never contains unmerged feature-branch work. In the incident that surfaced this, five agents dispatched from a feature-branch checkout all received a pre-feature-branch base; one attempted to "self-correct" with an unauthorized merge.

Before ANY worktree fan-out:

1. **Hygiene pass.** `git worktree list` + `git worktree prune`. Leftover `agent-*` / task worktrees from dead sessions get removed (check `git -C <wt> status` for uncommitted work first). `git fetch` so remote-tracking refs - including the base auto-isolation will use - are current.
2. **Choose the base explicitly.** If the work builds on anything other than fresh origin/main (a feature branch, unpushed commits, a pinned SHA), do NOT rely on `isolation: "worktree"`. Pre-bake: `git worktree add .worktrees/<task> -b <branch> <sha>` (adjust the path for your harness's convention), then dispatch WITHOUT the isolation option and pass the worktree path in the brief.
3. **Verify base after creation, before work starts.** Every worktree, auto or pre-baked: `git rev-parse HEAD` must equal the intended base SHA. For auto-created worktrees you can't inspect pre-dispatch, put it in the brief verbatim: "FIRST ACTION: confirm `git rev-parse HEAD` prints `<full sha>`; on mismatch STOP and report. Do not merge, rebase, or reset to self-correct."

**Symptom you missed this check**: agents report that files or functions named in the brief "don't exist in this checkout", or a returned diff re-implements work that already exists on the real base.

## Persistent warm worktree pools (projects with costly first-build state)

The section above is about getting an *ephemeral*, per-dispatch worktree onto the right base. This one covers the projects where an ephemeral worktree is the wrong tool at all.

Some projects carry per-checkout build state that costs 5-30 minutes to populate from cold: an installed `node_modules`, a Rust `target/`, a populated ccache or Gradle cache, a game engine's imported-asset database. A fresh worktree per agent pays that cost once per agent, and it dwarfs any wall-clock saving from running the lanes in parallel. On those projects, do **not** use `isolation: "worktree"`.

Instead, pre-provision a small pool of long-lived worktrees, each keeping its own warm build state, and have each agent check out the branch it needs inside an assigned slot:

```text
<repo>/
├── <main checkout>      <- the user's, warm
├── .worktrees/pool1/    <- pool slot, warm
└── .worktrees/pool2/    <- pool slot, warm
```

Each slot is created once with `git worktree add`, built once to populate its cache, and then reused across sessions. An agent runs `git fetch` and checks out its branch in the slot, and the incremental build touches only what actually changed.

**Rules for the pool:**

- **One agent per slot at a time.** Many toolchains take an exclusive per-checkout lock (a game engine's asset database, a build daemon's lockfile) and fail hard on a second concurrent process against the same directory. Even where they do not, two lanes sharing one working tree collide on the same files.
- **Reset and clean the slot before checking out a new branch** (`git reset --hard && git clean -fd`), so leftover state from a previous session cannot leak into the lane's diff. Do not add `-x`: it deletes the ignored build cache the pool exists to preserve.
- **Do not delete the slots between sessions.** Keeping the cache warm is the whole point.
- **Base verification still applies.** The prep checkout below is what sets the lane's base, so verify it exactly as the section above requires: `git rev-parse HEAD` in the slot must equal the intended base SHA before any work starts.
- **Only plans that branch into independent tasks benefit.** If task N+1 consumes symbols task N defines, run them sequentially in a single slot; parallelism buys nothing there.

### Reserving a slot

Slots are shared across sessions, so before doing anything in one, claim it with a reservation marker - otherwise a parallel session (or future-you) picks the same slot and stomps on in-progress work. The marker is the baseline mechanism and a complete protocol on its own - survey, claim, prep, release - needing no other tooling.

If the `project-lock` skill is installed, layer it in as an extra step. The two are complementary, not redundant: `.worktree-reserved` is an advisory sticky note saying "this slot is spoken for", self-documenting and surviving across sessions, so even an abandoned marker leaves a breadcrumb; `project-lock` is stronger write coordination that other agents' tooling checks before editing. A slot can carry a stale-but-harmless marker with no lock at all - but when a lock does exist, the lock, not the marker, is the authority on whether it is currently safe to write.

1. **Survey.** `git worktree list` to enumerate slots. For each candidate, check whether `<slot>/.worktree-reserved` exists and, if it does, read it for who holds it (`reserved-by`), when it was claimed (`reserved-at`), and whether it is past its own `stale-after` window; also read `git -C <slot> status -sb`. A slot is free if there is no marker, or the marker is past `stale-after` with no live owner (see the reaping note below) - **and** the working tree is clean. With `project-lock` installed, also run its `check <slot>` and treat a locked slot as not free even when it carries no marker. Detached HEAD at an older commit is fine: that is the pool's resting state.
2. **Claim.** Write `<slot>/.worktree-reserved` with content, not a bare touch, so the marker identifies who reserved the slot and leaves a breadcrumb if the session is abandoned:

   ```text
   worktree pool-slot reservation
   reserved-by: <agent harness and session id, e.g. "opencode session 7f3a9c12">
   reserved-at: <ISO 8601 UTC timestamp, e.g. 2026-08-04T21:40:00Z>
   branch: <branch about to be checked out>
   task: <plan/task reference>
   stale-after: 24h - if this is older and the owning session is gone, it is safe to delete
   ```

   `reserved-by` is harness-agnostic: any harness name plus its session or instance id (`claude-code session 83c40206`, `opencode session 7f3a9c12`), or `user@host` for a manual, non-agent reservation. `reserved-at` is always ISO 8601 UTC in `YYYY-MM-DDTHH:MM:SSZ` form, so markers compare correctly across machines in different time zones. One short file, never staged or committed (add `.worktree-reserved` to `.git/info/exclude` if it is not already globally ignored). Claiming can happen before or independently of acquiring a lock, because writing the marker does not touch the slot's tracked working tree.
3. **Lock (only if `project-lock` is installed).** Acquire the lock on the slot path, with a reason and a duration estimate, before anything in step 4 mutates the slot. A lock's jurisdiction is the nearest enclosing Git worktree, so each slot needs its own; acquiring on the repo root or a sibling slot does nothing for this one.
4. **Prep.** In the slot: `git fetch && git reset --hard && git clean -fd`, then `git checkout -B <branch> <base>`. This is the first step that actually mutates the slot, so a lock from step 3 must already be held before you run it. Verify the resulting base per the section above. The build cache stays warm, and the next build reimports only what changed.
5. **Release.** When the work is merged or abandoned: release the lock first if you hold one, then delete `.worktree-reserved` and reset the slot back to a clean detached state at the default branch, so the next session finds it warm and obviously free. When handing a branch off mid-stream instead, leave the marker in place (renewing or releasing the lock per the handoff) and update its `reserved-by` / `reserved-at` / `task` fields to describe the handoff.

**Reaping a stale marker.** A marker whose `reserved-at` is older than its `stale-after` (default 24h), with no activity on its named `branch` and no live `project-lock` on that slot (or no `project-lock` installed at all), belongs to a session that is probably gone. Reap it - delete the marker and treat the slot as free - but flag the reap to the user before overwriting the slot's working tree.

## Cross-repo dispatch mechanics

The parent skill covers parallel dispatch. Three cross-repo specifics:

- **One repo per agent - and one *session* per repo.** Two agents in the same repo (or one agent + an active parallel session) collide unless you pre-bake separate worktrees per session - see Pre-flight check above. Across different repos with no parallel sessions, isolation is automatic.
- **`isolation: "worktree"` is intra-repo only - it cannot help here.** It worktrees the *orchestrator's* repo, not the target. For cross-repo dispatch, pre-bake a worktree in the target via `git -C <target> worktree add ...` and pass the path to the agent in the brief.
- **Verify worktree isolation actually took.** If you requested `isolation: "worktree"` and the agent's result doesn't include a `worktreePath`, isolation silently failed and the agent worked on the parent tree. Assume cross-contamination and investigate before dispatching more parallel work. And even when it took, verify the BASE - see "Worktree hygiene + base verification" above.

## Result triage (the part the parent skill doesn't cover)

The parent skill's "review and integrate" step assumes you can read the diffs and decide. Across repos with rich domain context, that's not enough - agents will frequently return "needs human review" items that are actually answerable from the code, and forwarding them wastes the user's attention.

When agents return, **do not** forward open questions to the user. Instead:

1. For each "needs human review" item, decide if you can answer it yourself by reading code.
2. Read the relevant files. Check git history. Look at adjacent features. Trace data flow.
3. Form a recommendation backed by evidence (concrete `file:line` references).
4. Either apply the answer yourself, or surface it to the user **with your recommendation and the evidence**, not as a raw open question.

Only bubble up to the user when:

- The question is genuinely a product call (only they know which interpretation they meant), or
- Two interpretations are equally plausible after investigation, or
- The fix requires a destructive action you need authorization for.

Format for bubbling up:
> The agent asked X. I read `file.py:142` and `other.py:88` - Y is true, so option (b) is the right read. **Recommend**: revert and reissue with scope (b). Confirm?

This is the most important difference between this skill and the parent. The parent assumes a debugging context where the right answer is in the test output. Across repos with PLAN-driven work, the right answer is usually in code the agent didn't read because you didn't tell it to.

## Maintenance breadcrumbs

Maintenance state lives in each repo as `.maintenance.json`, which is **tracked**, not gitignored: it records which maintenance findings were actioned and which were deliberately rejected, and nothing regenerates that. The machine-wide excludes file ignores it, so a compliant repo's `.gitignore` carries an explicit `!.maintenance.json` negation (see `project-maintenance`'s `references/cross-project-config.md`, "Safe git clean + post-clean init"). project-tracker exposes two MCP tools:

- `mcp__project-tracker__get_maintenance_state(name)` - read one project's breadcrumbs
- `mcp__project-tracker__find_stale_maintenance(task_name?)` - find projects where a task is stale

Without the MCP server, read each repo's `.maintenance.json` directly (enumerating repos with `project-tracker list --json`, or from the repositories the user named) and compute staleness with git, e.g. `git rev-list <last_run_commit>..HEAD --count`.

Quick format reference (full schema in `references/maintenance-format.md`):

```json
{
  "version": 1,
  "tasks": {
    "push-latest": {
      "kind": "per-commit",
      "last_run_commit": "<full-sha>",
      "last_run": "2026-04-06T14:23:11Z",
      "status": "clean"
    },
    "stale-worktrees": {
      "kind": "time-based",
      "last_run": "2026-04-06T14:23:11Z",
      "interval_days": 30,
      "status": "clean"
    }
  }
}
```

**Staleness rules** (already enforced by project-tracker):

- `per-commit` → stale when `last_run_commit != git rev-parse HEAD`
- `time-based` → stale when `now - last_run > interval_days`
- A task with `status: "failed"` at the **same** HEAD is **not** stale - re-running a failed task at an unchanged commit will fail again. Wait for HEAD to move.

**Runner contract:**

1. Read current state with `mcp__project-tracker__get_maintenance_state(name)` or directly from disk.
2. Skip if `is_task_stale` returns False.
3. Do the work.
4. Write a new entry with `status` and either `last_run_commit` (per-commit) or `last_run` (time-based). Call `project_tracker.scanner.maintenance.write_maintenance_state` to persist the JSON. **Known helper defect**: the installed `write_maintenance_state` currently appends a *positive* `.maintenance.json` line to `.gitignore` - the opposite of this contract, which requires `.maintenance.json` tracked with a `!.maintenance.json` negation (see `references/maintenance-format.md`). After calling the helper, check `.gitignore` for a bare `.maintenance.json` line; if present, remove it, add `!.maintenance.json` in its place, and `git add .maintenance.json .gitignore`. Without project-tracker installed, skip the helper entirely: write the JSON entry directly, add the `!.maintenance.json` negation to `.gitignore` yourself, and `git add .maintenance.json`.
5. Never delete entries - overwrite in place.

## Workflows

### Maintenance pass

```text
1. mcp__project-tracker__find_stale_maintenance(task_name="push-latest")
   → ["myrepo", "site"]                  # other repos clean, skipped
2. For each stale project:
   - cd into repo
   - run the operation
   - update .maintenance.json on success or failure
3. Report: "pushed 1/2, site failed (no upstream)"
```

Re-running 5 minutes later returns only `["site"]`. Cheap.

### Feature pass

```text
1. Identify candidate tasks: mcp__project-tracker__list_projects() to enumerate the fleet
   (or `project-tracker list --json` without the MCP server; without project-tracker
   installed, work from the repositories the user names),
   then read PLAN.md from each candidate project.
2. Triage each (the four questions above). Drop reds, produce handoff prompts.
3. Present green/yellow shortlist to the user. Wait for approval.
4. For approved tasks: write rich briefs with pre-answered ambiguities.
5. Dispatch all approved agents in ONE message (per parent skill).
   One agent per repo.
6. When they return, triage results yourself before reporting.
7. For each "needs review" item: investigate, recommend, then ask.
8. Present final summary as a per-project status table.
```

## Cross-repo permission notes

- Subagents inherit this session's directory permissions, not the orchestrator's cwd.
- If a sibling repo isn't in the allowed list, the agent will hit Edit/Write denials. Tell the agent in the brief to STOP on denial, not work around.
- The user can preemptively grant access with `/add-dir <path>` or by listing repos in the agent's settings under a directory-permission allowlist (naming and location vary by harness).
- **Bash commands run with full user privileges regardless of `--add-dir`** - the directory sandbox only affects file tools, not shell.
- Empirically, sibling-repo file access often *just works* without `/add-dir` if the user's settings allow a parent directory. Don't assume - but don't over-engineer either. If denials happen, surface them; if they don't, proceed.

## Anti-patterns specific to fleet work

(Inherits all anti-patterns from the parent skill. These are additions.)

- Dispatching a feature pass without showing the user the shortlist first.
- Spawning a subagent for a task you haven't read the surrounding code for.
- Forwarding agent open questions to the user verbatim instead of investigating them yourself.
- Mixing maintenance and feature tasks in one pass.
- Spinning up a fresh worktree per agent on a project whose first build costs half an hour, instead of reserving a slot from a warm pool.
- Using `isolation: "worktree"` for cross-repo parallelism (it doesn't work that way - different repos are already isolated).
- Dispatching into a target repo without checking `git worktree list` and `git status` first. Other agent sessions may already be working there; the worktree-list output will show it.
- Letting an agent commit/push without orchestrator review.
- Trusting "all tests pass" as proof of correctness for semantic changes.
- Re-running maintenance tasks on projects that haven't changed since their last clean run.
- Writing a yellow risk note without being able to articulate exactly what worried you.
