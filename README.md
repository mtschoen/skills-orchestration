# Orchestration

Seven skills for what changes when work outgrows one agent, one machine, or one
budget.

Single-agent practice does not scale by repetition. The moment a second agent
starts work, a set of problems appears that no amount of care inside any one
agent can solve: two agents editing the same checkout, a worktree branched from
the wrong base, five green branches that pass individually and conflict on
merge, an expensive model doing work a cheap one would have done identically,
and a quota consumed before the week is.

These skills are about the seams between agents, machines, and budgets.

| Skill | The seam it manages |
| --- | --- |
| [`fleet-orchestration`](fleet-orchestration/) | Dispatching agents across many repositories without collisions |
| [`review-in-parallel-pipelines`](review-in-parallel-pipelines/) | The merge point, where independently-green branches meet |
| [`project-lock`](project-lock/) | Two agents wanting the same checkout |
| [`agent-remote`](agent-remote/) | Work that physically cannot run on this host |
| [`remote-handoff`](remote-handoff/) | Leaving this machine while the next session must still run on it |
| [`external-harness-routing`](external-harness-routing/) | Which provider's quota a job should spend |
| [`cost-estimator`](cost-estimator/) | What the quota actually went on |

The last two are a pair. `external-harness-routing` decides where work goes
before it runs; `cost-estimator` tells you afterwards whether that routing was
right. Neither is much use without the other, and both exist because agent
capacity is a metered resource that is easy to spend badly and hard to notice
spending badly.

`review-in-parallel-pipelines` earns its place by firing at the moment that
feels safest. Branches come back green, the build passes, and the reflex is to
merge and move on. That reflex is exactly when independently-correct changes
combine into an incorrect whole.

## Design notes

**Isolation is the default, not the fallback.** Most of the failure modes here
reduce to shared mutable state: one checkout, one branch, one lock. The
recurring answer is to give each agent its own, and to verify the base it
started from rather than trusting it.

**Verify the base, always.** A worktree created for an agent is not necessarily
branched from where you think. `fleet-orchestration` treats confirming the base
commit as a required step rather than a precaution, because the failure is
silent and the work is wasted before anyone notices.

**One dependency, declared.** Fleet-*wide* enumeration needs
[project-tracker](https://github.com/mtschoen/schoen-lab) installed, because
something has to know which repositories are yours. This is stated plainly in
`fleet-orchestration` rather than papered over with a fallback that does not
work. Everything else here, including all of the dispatch discipline, runs with
no such dependency.

## Installing

These skills are distributed through the
[skills-dev](https://github.com/mtschoen/skills-dev) umbrella, which carries
the installer and can mirror them into Claude Code, Codex, opencode,
Antigravity, and Hermes. Each skill directory here is self-contained: `SKILL.md`
at its root plus optional `references/`, `scripts/`, and `assets/`.

## Related families

- [skills-completion-discipline](https://github.com/mtschoen/skills-completion-discipline) -
  the gates that decide when an agent may say "done".
- [skills-working-method](https://github.com/mtschoen/skills-working-method) -
  habits applied while the work is happening.
