---
name: agent-remote
description: Use when delegating work to a different machine because of hardware, OS-specific tooling, locally-installed services, or anything else that physically can't be done on the orchestrator's host. Triggers include "I need to run this on the linux box," "the GPU server has the data," "verify on the production host," any task where verification requires touching a remote machine, or when about to chain multiple ssh commands for multi-step remote work.
---

# agent-remote

## Overview

Give an agent the **"open a terminal"** affordance for remote work, instead of forcing it to pipe each command over ssh. The wrapper spawns an agent session (claude, opencode, agy, pi, or codex) on the remote host, in an isolated git worktree, with a warm shell where iteration is natural and state persists across commands. Returns a structured JSON result.

**The pattern raw ssh produces:**

```text
ssh host 'cmd1' && ssh host 'cmd2' && ssh host 'cat > file' < local && ssh host 'cmd3'
```

Each call is a fresh shell. No persistent state. Quoting hell. Re-derives the environment every time. Works for very simple linear tasks; falls apart on iteration.

**The pattern this skill provides:**

```text
python agent-remote.py run --host user@host --repo-path ~/project --prompt "..."
```

One call. The remote agent works in a normal shell with full context until done.

## Choosing an Agent Harness and Model

By default, the script attempts to auto-detect your current agent platform using active environment variables (e.g., defaulting to `agy` if running in an Antigravity session, or `opencode` if running in an opencode session). If it cannot detect the environment, it defaults to `opencode` to minimize API token costs.

However, **you are not restricted to the hosting environment's default.** You can cross-delegate to other agents or models using the CLI arguments:

- Use `--agent <claude|opencode|agy|pi|codex>` to explicitly choose the remote execution framework.
- Use `--model <model>` (or `-m`) to override the LLM model used by the remote agent (supported by `opencode`, `agy`, `pi`, and `codex`).

## When to use this skill

- The task touches hardware that only exists on the remote (GPUs, sensors, large disks).
- The task touches services managed on the remote (systemd units, databases, running daemons).
- Verification requires real OS-level state on the remote (`/proc/*`, `/sys/*`, `journalctl`, `systemctl`).
- The work involves more than 2-3 round-trips of edit→run→inspect on the remote.
- You're about to type a multi-line `ssh host '...'` command with embedded heredocs or `&&` chains.

**Don't use this skill for:**

- One-off "tell me the hostname" probes - just use single-command ssh.
- Pure-Python work that's been mocked correctly (unit tests with mocked subprocess calls don't need real hardware).
- Work where the remote and local hosts have the *same* tooling and the only difference is the codebase - just edit locally.

## Critical: how code flows between local and remote

**The wrapper does NOT push your local changes to the remote, and does NOT pull remote-authored files back.** It creates a worktree on the remote starting from the *remote's existing HEAD*. This is the most common pitfall - three independent verification runs all rediscovered it.

The model is:

- The wrapper hands a fresh worktree on the remote to an agent session.
- That session works in its own warm shell, on the remote's git state, and may commit to the worktree branch.
- When done, the wrapper returns the branch name and SHA. Your local clone has not been touched.

**If your task needs your local changes on the remote, push first.**

```bash
git push origin <your-branch>
ssh user@host 'git -C /path/to/remote/repo fetch'
# (or rely on the remote's checkout pulling main on its own schedule)
```

**If you need the remote-authored files back in your local tree, fetch after.**

```bash
git fetch <your-remote> agent-remote/<branch-name>
git checkout agent-remote/<branch-name>     # to inspect
# or:
git cherry-pick <sha>                        # to merge specific commits
git merge agent-remote/<branch-name>        # to merge the whole branch
```

### Edge case: detached HEAD or no shared remote

If your local clone is in **detached HEAD** state, `git push` won't work without naming a branch. Create one first:

```bash
git switch -c local-changes-for-agent-remote
git push origin local-changes-for-agent-remote
```

If your local clone has **no shared git remote with the target host** (e.g. you work over a private LAN with no shared Git host between them), the simplest workaround is to put the file content in the prompt itself:

```bash
python agent-remote.py run --host ... --prompt "Create a file at src/foo.py with this exact content:
<paste full file content here>
Then run pytest and report results."
```

That's slower for large files but avoids the need for a shared git remote entirely. **Don't use this for files larger than a few hundred lines** - prompt token cost dominates.

If you have **a shared git remote but the remote checkout doesn't track it**, ssh in once and `git remote add origin <url>` on the remote checkout. One-time setup.

**Or, simplest of all:** treat the agent-remote task as fully self-contained. Have it clone what it needs from upstream, do the work, write its own files in its own worktree, and report results textually. Pull nothing back. Most "verify on the remote" tasks fit this shape.

## Critical: framing prevents silent drift

When delegating cross-platform work, agents will rationalize *"I'll write the code and trust it works on the other platform - verifying is too much trouble."* This is silent drift. To prevent it, **the prompt you pass to the wrapper must explicitly forbid unverified deliverables.** Required phrasing pattern:

> "You must actually run this end-to-end on the remote and include the real output in your report. Do not paste hypothetical examples. If the output isn't in the report, the task isn't done."

Baseline testing showed that agents who saw this framing in their prompt resisted drift; agents who didn't, drifted. The wrapper itself does not enforce this - it's the caller's job to embed it in the `--prompt`.

## Quick reference

```bash
# Probe a remote: verify ssh, agents, git, and the repo path are reachable
python agent-remote.py probe --host user@host --repo-path /path/to/repo [--os auto|windows|posix]

# Run a task in a fresh remote worktree, returning structured JSON
python agent-remote.py run \
  --host user@host \
  --repo-path /path/to/repo \
  --prompt "Build the thing, run it, paste the real output." \
  [--branch agent-remote/my-task] \
  [--permission-mode acceptEdits] \
  [--agent opencode] \
  [--model ollama/qwen3.5-9b] \
  [--os auto|windows|posix] \
  [--extra-allow "Bash(sudo systemctl *)"]

# Clean up a worktree+branch on the remote when you're done
python agent-remote.py cleanup --host user@host --repo-path /path/to/repo --branch agent-remote/my-task [--os auto|windows|posix]
```

The `run` output is JSON on stdout: `{success, branch, worktree_path, parent_commit, new_commit, files_changed, agent_exit_code, stdout_tail, stderr_tail, cleanup_command}`. Parse it.

## Install

**Local (per project that uses the skill):** add one permission rule to `.claude/settings.local.json` or `.gemini/settings.local.json` so subagents can invoke the wrapper without per-call prompts:

```json
{
  "permissions": {
    "allow": [
      "Bash(python *agent-remote.py *)"
    ]
  }
}
```

**Remote (per host you'll target):**

1. **Target agent CLI must be installed.** E.g., `claude`, `opencode`, `agy`, `pi`, or `codex`. The wrapper's `probe` subcommand reports their presence.
2. **An ssh key authorized for passwordless login.** `ssh-copy-id user@host` once.
3. **An existing checkout of the repo** at a known path on the remote. The wrapper creates a *sibling* worktree under `<parent-of-repo>/agent-remote-worktrees/`; it does not modify the existing checkout.

When using `claude` as the remote agent, the wrapper writes a narrow `.claude/settings.local.json` into each remote worktree before launching it, so it has Bash/Edit/Write/Read/Glob/Grep without needing `--permission-mode bypassPermissions`. Note: "narrow" means few tools, not low power - the seeded allowlist includes unrestricted Bash inside the remote worktree; see "Permission modes" below for the full caveat and the tightening knob. For `opencode` and `agy`, permissions are bypassed/approved automatically using `--auto` or `--dangerously-skip-permissions` flags.

## Common mistakes (each one was discovered the hard way)

| Mistake | What happens | Fix |
|---|---|---|
| Using raw `ssh host 'cmd'` chains for multi-step work | Quoting hell, lost state between calls, slow iteration | Use this wrapper instead |
| Writing the local half of a task in your live working tree | Pollutes the live tree with parallel-implementation duplicates of files the remote also wrote | Use a local worktree (`git worktree add ...`) for the local half OR delegate the whole task to the remote |
| Expecting the remote to see your local uncommitted changes | The remote starts from its own `git HEAD` - your local working state is invisible to it | `git push` your branch first, then `ssh host 'git -C /repo fetch'`. See "How code flows between local and remote" above |
| Expecting the wrapper to bring remote-authored files back to local | The wrapper returns SHA + filenames but does NOT pull files | `git fetch <remote> <branch-name>` after the run completes, then merge/cherry-pick. See edge cases above |
| Trying `scp` to push files to the remote | Often denied by sandbox harnesses | Wrapper writes files via stdin redirect - handled |
| Assuming `~/.local/bin` is on remote PATH | Agent, pipx tools, npm-globals "not found" in non-interactive ssh | Wrapper prepends `~/.local/bin:~/.npm-global/bin:~/bin` - handled |
| Running `nvcc`/`cmake`/`conda` over plain `ssh host 'cmd'` | Mysterious "not found" because non-interactive PATH lacks `/opt/cuda/bin` etc. | Wrapper uses `bash -lc` so login-shell PATH applies - handled |
| Passing remote paths from Git Bash on Windows | MSYS converts `/home/x` to `C:/Program Files/Git/home/x` in argv before Python sees it | Wrapper detects and reverses the prefix - handled. As fallback, pass `//home/x` or set `MSYS_NO_PATHCONV=1` |
| Hardcoding `~/project/.venv/bin/python` in unit files | Many remote checkouts have no venv (editable-installed system-wide) | Probe first; use `python3` or `command -v python3` |
| `systemctl --user` without `loginctl enable-linger` | Timer fires only while user has an active session | Document the linger requirement; don't try to escalate |
| Skipping verification "because the code is obviously correct" | Silent drift, ships unverified code | Embed the framing pattern from "Critical: framing prevents silent drift" above into every prompt |
| Calling the wrapper without `--branch` and forgetting to clean up | Worktrees pile up on the remote | Capture `cleanup_command` from the JSON result and run it when done |
| Targeting a Windows host with WSL installed | `bash -lc` executes inside WSL VM rather than on native Windows | Wrapper auto-detects remote OS and runs commands via the native shell on Windows (cmd.exe/PowerShell). Explicit override: `--os windows` |
| Trusting `new_commit` / `files_changed` when the remote agent uses nested skills | An agent session that invokes nested superpowers skills (`writing-plans`/`executing-plans`) may commit to refs other than the worktree HEAD; the wrapper only sees worktree HEAD changes | Ask the remote prompt to summarize what it committed, OR use `git fetch` to inspect all refs on the branch directly. See "Known limitations" |

## Example

```bash
python agent-remote.py run \
  --host user@remote-host \
  --repo-path /home/user/myrepo \
  --branch agent-remote/nvbw-2026-04-07 \
  --agent opencode \
  --model ollama/qwen3.5-9b \
  --prompt "Clone https://github.com/NVIDIA/nvbandwidth into ~/nvbw-build, build it with cmake, run \`./nvbandwidth\` against all available GPUs, parse the host-to-device and device-to-device matrices, and write the result to /tmp/nvbw-result.json. You MUST run this end-to-end on this machine and include the real numeric matrix in your final report. Do not paste hypothetical examples. If the matrix isn't in the report, the task isn't done."
```

Returns (abbreviated):

```json
{
  "success": true,
  "branch": "agent-remote/nvbw-2026-04-07",
  "worktree_path": "/home/user/agent-remote-worktrees/agent-remote_nvbw-2026-04-07",
  "files_changed": ["scripts/nvbw_runner.py", "tests/test_nvbw_runner.py"],
  "agent_exit_code": 0,
  "stdout_tail": "...real matrix output...",
  "cleanup_command": "python agent-remote.py cleanup --host user@remote-host --branch agent-remote/nvbw-2026-04-07"
}
```

The orchestrator can then `git fetch && git merge agent-remote/nvbw-2026-04-07` from the remote, inspect the changes, and run the `cleanup_command` when done.

## Permission modes

Default: `acceptEdits`.
When using `claude`, the wrapper seeds an allowlist into the worktree's `.claude/settings.local.json` so the remote session can use Bash, Edit, Write, Read, Glob, Grep, WebFetch, and WebSearch without prompts. The allowlist is narrow in tool count, not in power (see the caveat below). If a task needs additional permissions (e.g. `sudo` for system installs), pass `--extra-allow "Bash(sudo apt install *)"` to widen the allowlist for that specific run.

When using `opencode` or `agy`, permission requests are auto-approved via `--auto` or `--dangerously-skip-permissions` for default/acceptEdits/bypassPermissions modes.

`bypassPermissions` is supported but **refused unless the env var `REMOTE_AGENT_ALLOW_BYPASS=1` is set on the orchestrator host.**

**Honest caveat:** the default allowlist grants unrestricted `Bash`, not a set of scoped `Bash(cmd *)` patterns. A git worktree isolates the checkout/branch; it does not sandbox the filesystem or the OS. So on the default allowlist, `acceptEdits` gives the remote session nearly the same reach as `bypassPermissions` would: it can read `~/.ssh`, exfiltrate data, or `rm -rf` anything the ssh user can reach, gated only by the env-var check above for the `bypassPermissions` *mode itself*, not for what an unrestricted `Bash` entry can already do under `acceptEdits`. Treat `host` as a machine you already trust with full shell access: this wrapper does not add a security boundary on top of that trust.

To tighten it: there's no CLI flag that narrows the allowlist (`--extra-allow` only widens it). Edit `DEFAULT_REMOTE_ALLOWLIST` in `references/agent-remote.py` directly, e.g. replace the bare `"Bash"` entry with the specific `Bash(cmd *)` patterns your tasks actually need. Narrowing the shipped default is a separate decision (it needs real-world testing against the tasks this wrapper is used for); this note just documents the knob.

## Known limitations

These are real, observed during verification testing, and not yet fixed in the wrapper. Work around them; don't be surprised by them.

- **`new_commit` and `files_changed` may report null/empty even when the remote committed.** Observed when the remote agent invokes nested superpowers skills (`writing-plans`, `executing-plans`) that commit to internal refs the wrapper doesn't introspect. The wrapper only checks the worktree's HEAD; nested skill commits land elsewhere on the branch's history. Workaround: always have the prompt include a final step like *"After committing, print the SHA you just made."* Or fetch the branch back and `git log parent..HEAD` to see everything.
- **`stdout_tail` is bounded** at 20000 chars. For verification tasks that produce a lot of output (full benchmark runs, journald excerpts, multi-step systemctl status), instruct the remote prompt to also write key output to a file inside the worktree, then either (a) `cat` it back in a follow-up `run` invocation or (b) `git fetch` the branch and read the file from your local checkout.
- **No streaming.** A 600s task is opaque until it returns. For long verifies, watch the wrapper's output file directly if you need progress: `tail -f <output-file>`.
- **No `--auto-cleanup`.** You must call `cleanup` explicitly. Capture the `cleanup_command` field from the run result and run it before considering the task done.
- **No two-way file sync.** The wrapper does not push local changes to the remote, and does not pull remote-authored files to local. Use `git push`/`git fetch` for that. See "How code flows between local and remote" above.

## What this skill explicitly doesn't do

- **Doesn't manage authentication.** ssh keys + agent CLI auth must already work on the remote.
- **Doesn't push code to the remote.** It expects an existing checkout. Use `git push` and let the remote `git fetch` if you need a specific commit.
- **Doesn't merge results back.** It returns the branch name; you decide whether to fetch+merge, cherry-pick, or discard.
- **Doesn't enforce framing against silent drift.** That's the caller's responsibility. The skill can only document the pattern.
- **Doesn't replace local work.** Use this when verification *requires* the remote, not as a default.
