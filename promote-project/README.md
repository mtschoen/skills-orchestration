# promote-project

A skill that turns a bare idea folder (or a brand-new project) into a real git project - git history, base config files, stack-aware linters and on-save hooks, an `aislop` quality gate, `AGENTS.md`, a language manifest, a LICENSE, CI, and remote hosting on Gitea + GitHub - then registers it in project-tracker.

## When it fires

After a brainstorm has fixed the tech stack and the user signals they want to start real work: "promote this to a real project", "scaffold this", "set up the bells and whistles", "make this a real repo", "wire up the linters and hooks".

It is the step *after* `capture-idea`: brainstorm → capture-idea (bare folder) → **promote-project** (real, compliant repo) → coding. It can also adopt an already-created folder that skipped the capture step.

## What it does

Runs in two tiers. **Tier 1 (deterministic base)** is language-agnostic and always the same: `git init`, `.editorconfig`, `.gitattributes`, `.gitignore`, a `README.md` stub, `.plan`. **Tier 2 (stack-aware)** is decided from the project's chosen stack: linter config + a `PostToolUse` on-save lint hook, `aislop` wiring, `AGENTS.md` (+ `CLAUDE.md` and `GEMINI.md` `@AGENTS.md` pointers), the language manifest (e.g. `pyproject.toml`), CI, and an MIT `LICENSE`. It then creates the Gitea + GitHub remotes and registers the project with `project-tracker project create <name> --json` or `project-tracker project register <path> --json`.

If the user maintains `~/.agents/project-conventions.md`, the compliance definition is sourced from it, plus this skill's own `references/config-shapes.md` for the literal config shapes - promote-project (birth) and `project-maintenance` (upkeep) apply the same conventions independently.

The authoritative spec is [`SKILL.md`](SKILL.md).

**Repo:** <https://github.com/mtschoen/skills-promote-project>
