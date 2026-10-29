# capture-idea

A skill that externalizes a brainstorming conversation into a durable, project-tracker-indexed project folder - turning "we were just talking about a cool idea" into a folder on disk with the conversation substance, an elevator pitch, and a starter plan.

## When it fires

The user signals they want to save an idea: "capture this as a project", "let's start a folder for this", "jot this down as an idea", "put this somewhere I won't forget", "I want to come back to this later".

It sits between `superpowers:brainstorming` (which explores an idea) and `superpowers:writing-plans` (which produces an implementation plan): brainstorm → capture-idea → later maybe → writing-plans.

## What it does

Confirms intent, proposes a kebab-case folder name, creates the folder with `project-tracker project create <name> --json`, and writes summary files derived from the conversation (typically `README.md`, `BRAINSTORM.md`, `PLAN.md`).

The authoritative spec is [`SKILL.md`](SKILL.md).

**Repo:** <https://github.com/mtschoen/skills-capture-idea>
