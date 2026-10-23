---
name: review-in-parallel-pipelines
description: "Use when 2+ concurrent implementer agents on isolated branches or worktrees reach a merge point, return green reports, finish a round, or move to the next phase. Requires per-branch and integrated round-boundary review before proceeding; single-agent-at-a-time runs do not need it."
---

# Review in Parallel Pipelines

## Why this skill exists

Running N agents concurrently is an *execution* pattern. Review is *orthogonal* to it - going faster does not review anything. When the orchestrator is busy dispatching and watching N branches, the cheapest local move is to merge each branch the moment its report says "green" and push on to the next round. That is precisely when silent-weird solutions land in the merged product.

**The failure mode this skill prevents:** the orchestrator declares *"all branches merged, no conflicts, build green - clear to proceed"* without anyone ever looking at the **integrated** result for cross-branch problems. The branches each pass; the product is quietly broken or bloated.

**The failure mode this skill must NOT create:** a paranoid orchestrator that blocks clean merges, manufactures problems, or treats review as a pipeline-stalling ritual. Most branches approve and most rounds need no refinement - the value is the check, not the friction.

This is **orchestrator -> branches** review. It sits on top of `superpowers:subagent-driven-development` (which covers per-task review for one agent at a time) and adds the layer that pattern lacks: review across *many concurrent* agents per phase.

## When this fires / when not

Use when orchestrating 2+ implementer agents in parallel against isolated branches or worktrees and you reach a merge point - branches returning with green reports, deciding what to merge, declaring a round done, or moving to the next parallel phase. Fires on the reflex to merge-on-green and move on (subagent-driven-development parallel mode, dispatching-parallel-agents, fleet orchestration). The moment that feels like 'all branches merged, build green, clear to proceed' is exactly when this applies. Single-agent-at-a-time runs don't need it.

Fires for any execution pattern with **>=2 concurrent implementer agents** landing into isolated branches/worktrees:

- `superpowers:subagent-driven-development` run in parallel mode
- `superpowers:dispatching-parallel-agents` fan-out
- `fleet-orchestration` when per-repo work has similar review needs

Does **not** add value when only one agent runs at a time - `subagent-driven-development`'s existing two-stage review is sufficient. If you're not merging the output of concurrent agents, skip this.

**Fork note:** the `subagent-driven-development` parallel mode listed above assumes the superpowers fork at <https://github.com/mtschoen/superpowers>. Official superpowers 6.2.0 has no parallel-implementer mode - it says "Never dispatch multiple implementation subagents in parallel." On official superpowers, this skill's trigger comes only from `dispatching-parallel-agents` fan-out or `fleet-orchestration`, not from `subagent-driven-development`.

## The trap: a clean merge is not a clean integration

This is the core discipline. Internalize it before anything else.

> **"Zero conflicts, disjoint file sets, green build, all tests pass" proves only that git could combine the text and that each branch works in isolation. It says nothing about whether the integrated whole is right.**

An entire class of defects produces **no merge conflict and no test failure**, so the merge tool and the build are blind to it:

- **Duplicated logic** - two branches independently implement the same helper (the canonical halo). Disjoint files, clean merge, green tests - and now the same function exists twice.
- **Shadowed / dead code** - branch A's version of a thing silently overrides branch B's; a stub one branch left gets shadowed by another's real implementation.
- **Redundant dependencies** - branch X adds a package that overlaps branch Y's; both coverage exclusions could consolidate.
- **Architectural drift** - the round's combined work bulged past the planned shape, even though each branch stayed small.
- **Aggregated open threads** - several branches each left a "clean this up later"; individually fine, collectively a mess.

A clean octopus merge with green CI is the **most** dangerous moment, not the safe one - it is exactly when "clear to proceed" feels justified. If your summary contains the words *"no conflicts"* or *"disjoint file sets"* as evidence that nothing needs review, STOP: those are evidence the merge tool was satisfied, not that the integration is correct.

## The three review checkpoints

| Checkpoint | When | Looks at | Output |
|---|---|---|---|
| **1. Per-branch** | before each merge | that one branch's `git diff <merge-base>..<branch>` vs. its assigned spec | `APPROVED` -> merge; `CHANGES_REQUESTED` (list) -> re-dispatch; `ESCALATE` -> handle manually |
| **2. Round-boundary** | after *all* merges in a round | the **integrated** tree, branches compared against each other | a refinement punch-list (possibly empty) |
| **3. Refinement** | before the next planned phase | the punch-list from (2) | scheduled cleanup tasks |

**Checkpoint 1 - per-branch.** Dispatch a cheap reviewer agent (sonnet) per returned branch with its diff, its spec section, its assigned tasks, and its final report. Check spec compliance, `escalate-over-shortcut` red-flag shapes, cross-boundary smell (test code in prod paths or vice-versa), and **hidden deviations** the report didn't disclose. A competent agent often does this instinctively for *blatant* single-branch smells - formalize it anyway so it isn't skipped under volume.

**Checkpoint 2 - round-boundary (the one per-branch review cannot do).** After the round's branches all land, review the integrated state by **comparing branches against each other** - this is the only checkpoint that catches the cross-branch class above, because no single branch's diff reveals a halo it co-created. This is where the "clean merge != clean integration" discipline cashes out.

**Checkpoint 3 - refinement as a scheduled phase.** Treat the punch-list as the next round's work, dispatched *before* the next planned feature phase. Refinement is first-class, not an afterthought. These rounds are usually small (1-3 tasks) and often serial; don't let them compete with feature phases.

Reviewer dispatch templates and the full round-boundary checklist live in `references/`.

## Review scales WITH parallelism, not against it

The more branches in flight, the stronger the pull to merge-on-green and move on - **and** the more cross-branch surface area for halos. These move together. "We're behind, six branches to land, no time to review" is the exact condition under which review matters most. **If you're too busy to review, you're too busy to merge.** Hold the un-reviewed branches rather than declaring the round done.

## Tell implementers a reviewer is coming

Add one line to every parallel implementer dispatch: *a reviewer agent will examine your branch's diff against your spec before it merges.* Agents document deviations more honestly and avoid red-flag shapes when they know the review is certain. Snippet in `references/review-dispatch-templates.md`.

## Cost

~N per-branch reviewer runs plus ~1 round-boundary run per round; sonnet keeps each modest. Break-even is one caught cross-branch issue per round - and empirically (a Windows-to-Android-XR streaming project's sessions, rounds of 2-14 agents) every round produced at least one. Strongly positive ROI.

## Red flags - you are about to skip review

| Thought | Reality |
|---|---|
| "Merged with zero conflicts - clear to proceed." | No conflict != correct integration. Cross-branch halos never conflict. |
| "Disjoint file sets, nothing to review." | Disjoint files routinely duplicate logic or shadow each other. That *is* the halo. |
| "Build's green, all tests pass." | Green proves each branch works alone; silent on cross-branch duplication, drift, or a quietly-lowered gate. |
| "We're behind, N branches to land." | More branches = more halo surface. Review scales with parallelism. |
| "Each agent reported success." | A report is self-assessment; the halo is invisible to the branch that caused it. |
| "I'll catch it in the next phase." | The next phase builds *on* the un-reviewed integration; drift compounds. |

## Self-check before declaring "clear to proceed"

- Did I review each branch's diff before merging it - not just trust the green report?
- After all merges, did I look at the **integrated** tree *across* branches: duplicate helpers, shadowed defs, redundant deps, dead stubs, architectural drift?
- Did two branches independently solve the same sub-problem? (the canonical halo)
- Are coverage/build/tests green for the *right* reasons - no silently-skipped test, no lowered threshold?
- Any "clean this up later" notes spread across branches that should become a refinement round *now*?

If a round-boundary issue exists: **schedule a refinement round before the next phase - do not declare "clear to proceed."** If all checks pass: proceed.

## What this is NOT

- **Not a replacement for `subagent-driven-development`'s per-task review.** That covers one agent at a time; this is the multi-agent-per-phase layer on top.
- **Not `pushback`** (agent -> user) and **not `escalate-over-shortcut`** (agent -> self, on the agent's own draft). This is orchestrator -> branches, on *other* agents' returned work. The reviewer agents you dispatch enforce `escalate-over-shortcut`'s red-flag list.
- **Not paranoia.** Rubber-stamping clean branches and catching the occasional halo is the skill working correctly. Manufacturing problems or blocking clean merges is the failure mode the control guards against.
