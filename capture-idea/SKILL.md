---
name: capture-idea
description: Use when the user wants to save a brainstorming conversation as a new project folder, asking to "capture this as a project" or "jot this down as an idea". Creates a project-tracker folder with summary files derived from the conversation so the idea can be resumed later.
---

# capture-idea

Externalize a brainstorming conversation into a durable, project-tracker-indexed folder. This skill turns "we were just talking about a cool idea" into a folder on disk containing the conversation substance, an elevator pitch, and a starter plan.

## When to use

Use when the user wants to externalize a brainstorming conversation into a new
project folder. Creates the folder with `project-tracker project create example --json`
and writes summary files derived from the conversation substance.

Triggers are the user saying things like:

- "capture this as a project"
- "let's start a folder for this"
- "jot this down as an idea"
- "put this somewhere I won't forget"
- "I want to come back to this later"

The skill is distinct from `superpowers:brainstorming` (which explores an idea conversationally) and from `superpowers:writing-plans` (which produces an implementation plan from an approved spec). It sits between them: brainstorm -> capture-idea -> later maybe -> writing-plans.

## Relationship to the brainstorming HARD-GATE

The `superpowers:brainstorming` skill's HARD-GATE forbids invoking implementation skills before a spec is approved. `capture-idea` is NOT an implementation skill. It writes notes, not code. It produces no spec for implementation - it externalizes conversation substance so it isn't lost. Invoking `capture-idea` during or after a brainstorm does not violate the gate.

## Workflow

1. **Confirm intent.** Pose a single yes/no: "Capture this as a project under `<proposed-name>`? Or keep going first?" Avoid premature externalization when the user is still mid-thought.

2. **Propose folder name and parent dir.** Derive a kebab-case folder name from the core idea. Use the default parent dir unless the user specifies otherwise. Let the user override either.

3. **Create and register the folder.** Run `project-tracker project create <name> --parent-dir <dir> --description <text> --json`.
   - Required: `name`
   - Optional: `parent_dir`, `description` (one-line), `status` (defaults to `incubating`)
   - Returns: `{path, project_id, status, warning}`. If `warning` is non-null, relay it to the user verbatim.
   - **If project-tracker is not installed**, create the folder yourself (`mkdir -p <parent_dir>/<name>`) and tell the user it is not indexed. Steps 4 and 5 still apply: the written files are the durable artifact.

4. **Write files at your judgment.** No rigid trio. The three files below are strong defaults - include them unless the conversation clearly doesn't call for them, and add others (e.g. `RESEARCH.md`) if useful:
   - **`README.md`** - one-paragraph elevator pitch, current status, table of contents pointing to sibling files.
   - **`BRAINSTORM.md`** - the thinking log. Arguments, pushback from both sides, tangents, half-baked threads, open questions. Near-verbatim from the conversation, not sanitized into executive-summary prose. *This is the high-value artifact - don't flatten it.*
   - **`PLAN.md`** - open tasks in project-tracker's checkbox format, including research questions. Writing this is the payoff for project-tracker's existing plan detection.

5. **Report back.** Return:
   - Folder path
   - Files written (list)
   - project-tracker project ID, when the project was registered
   - One-line "next step" suggestion (continue brainstorming, pick up later via `find-task`, etc.)

## Errors from project creation

If the command fails:

- **ValueError / FileExistsError** - relay the message to the user and offer to retry with a different name or parent dir. Do not retry automatically.
- **warning** in the return dict - relay verbatim so the user knows the folder won't be auto-rediscovered during `project-tracker scan`.

## Out of scope for this skill

- Git init (the `promote-project` skill covers this)
- Language scaffolding, LICENSE, .gitignore, linters/hooks, CI, remotes - `promote-project` handles all of it once the stack is decided
- Moving existing files into the new folder
- Editing existing idea folders (regular file editing is fine)
- Templates - for v1, file choice is pure LLM judgment

## PLAN.md format (project-tracker integration)

To flow into project-tracker's existing plan detection, write `PLAN.md` with checkbox syntax:

```markdown
# <project-name> - Plan

## Inbox

- [ ] First open task or question
- [ ] Second task
- [ ] Research: link to prior art X
```

`project-tracker find-open-tasks --json` will pick these up on the next scan.
