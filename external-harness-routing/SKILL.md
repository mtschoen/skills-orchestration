---
name: external-harness-routing
description: "Use when conserving primary-harness quota, reaching unavailable models, inventorying installed agent CLIs, planning fan-out on other pools, or before hand-rolling headless CLI calls. Inventories and routes work by tier to other harnesses with tracked dispatch and report files."
---

# external-harness-routing

## When to use

Use when diverting work from the metered primary harness to other installed agent CLIs to conserve its quota or to reach models it does not offer. Triggers include "run this on a cheaper harness", "save my Claude tokens", "use another subscription for this sweep", "which agent CLIs are installed here", fan-out jobs where built-in subagents would burn the scarce pool, or before hand-rolling headless CLI calls to claude, codex, gemini, agy, opencode, kimi, qwen, pi, or muse.

## Overview

A multi-provider setup pays off only if work actually lands on the harness whose quota is cheapest for it: subscription plans are subsidized, each provider's pool is billed separately, and some proprietary models exist in only one place. This skill covers the same-machine half of that routing: discover which agent CLIs are installed, pick the right lane by capability tier, and dispatch well-specified work to them as headless prompts with a reliable return channel.

Scope boundaries:

- Work that must happen on a **different machine** (hardware, OS tooling, local services): use the `agent-remote` skill instead.
- Fan-out across **multiple local repositories**: use `fleet-orchestration`, which also carries the fuller version of the tier-routing rules.
- **Same-harness** lanes on an acceptable pool: built-in subagents (`superpowers:dispatching-parallel-agents`) are simpler - external dispatch adds a report-file indirection, so use it when the pool economics or a model you lack justify it.

## Step 1: inventory what is installed

Never assume a harness exists or is logged in. Probe first:

```bash
for cli in claude codex gemini agy opencode kimi qwen pi muse; do
  command -v "$cli" >/dev/null 2>&1 && echo "$cli: $(command -v "$cli")"
done
```

Then verify auth on each harness you plan to use with a cheap probe: a one-line "Reply with exactly: OK" prompt under a short timeout. Three fleet-verified auth facts:

- A harness's **first call after idle can fail transiently** (exit 1, empty output). Retry once before concluding breakage.
- **Expired OAuth cannot be fixed headlessly** (e.g. a managed-provider login that wants an interactive `login` command). Report it to the user and route around that harness; do not burn budget retrying.
- **Quota exhaustion often fails silently**: the run produces no output and just hangs. If a fresh dispatch produces nothing within a few minutes and hang detection (below, including the harness's own session record) confirms no activity, assume exhaustion, kill it, and re-route the lane to another pool.

## Step 2: pick the lane by tier, not by model name

Think in capability tiers (premium / mid / small), not vendor model names - each harness maps a tier to its own concrete model id, and that mapping belongs in your rules or memory layer, not in task briefs. The routing rules:

- **The scarcest tier holds the orchestrator seat only**: triage, briefs, synthesis, review, and delicate or irreversible steps. If your own session runs on a capped pool, budget it explicitly (e.g. keep the premium tier to about half the session's spend) and divert the rest.
- **Strong-tier lanes need not come from the orchestrator's pool.** When another provider offers a premium-class model on a pool with more headroom, route demanding single lanes (review, hard implementation) there instead of escalating on the scarce pool; probe the harness first (Step 1) and keep the lane's scope and report-file contract unchanged.
- **Mid-tier is the default for well-specified lanes**, including feature implementation, multi-file refactors, and test work - not just search and mechanical edits. Modern mid-tier models are a marked step up from the ones older "mechanical only" advice was calibrated on.
- **Sustained-volume work** (drains, sweeps, big fan-outs) goes to whichever pool is effectively unmetered for you - but confirm it first: some "infinite" subscriptions have weekly credit caps that fail silently (see Step 1).
- **Read pool headroom before a fan-out.** Where inference-manager is installed, `inference-manager quota --json` reports each provider pool's state, remaining `headroom_fraction`, and per-window `resets_at` (plus `fetched_at`); derive burn pace from the elapsed fraction of each window (`five_hour`, `seven_day`/`weekly`, `monthly`) and send the volume to pools with margin at reset.
- **Pass an explicit model on every dispatch.** A lane that inherits the orchestrator's model by omission silently burns the scarce pool on work the mid tier handles fine.
- **Escalate a single lane, never the fleet**, and only for ambiguous architecture, an embedded product call, or a delicate step that cannot be pre-answered in the brief.
- **Nested headless calls to your own metered harness cost full sessions each.** A sweep is items x configs x runs sessions; do the multiplication before launching, not after the pool is gone.
- **Delegate inline grunt work** (log forensics, fan-out searches, mechanical edits) instead of doing it in the orchestrator seat - "it is quick, I will just do it here" is the failure mode, not a shortcut.

## Step 3: the headless dispatch pattern

Each point below was learned from a real failure. The pattern:

1. **Write the full prompt to a file** and have your driver read it from disk. Inline prompts rot under shell quoting and are impossible to review before dispatch.
2. **Run the dispatch as a tracked background task with a hard `timeout`.** Headless runs regularly outlive foreground tool-call limits, and a harness hang (below) must not strand you.
3. **The report file is the return channel.** Require the agent to write its findings to a report file at an absolute path you choose. Stdout is bounded, buffered, or lost; "the report file does not exist" is also your cheapest failure signal.
4. **Absolute paths everywhere in the prompt.** Several CLIs anchor relative paths against their own launch directory, not your cwd.
5. **Every prompt carries:** "If any referenced file is missing, STOP and report BLOCKED naming it." Headless agents silently improvise from prompt context when an input file does not exist instead of flagging it. Verify every file you reference exists before dispatching.
6. **Scope hard:** "Work ONLY in this directory. Do not edit live config, global settings, or anything outside it." An unscoped lane once rewrote the operator's live harness config.
7. **Attribution:** commits made by an external harness land under the repo's git identity. Require the actual harness and model in a `Co-Authored-By` trailer so the history stays attributable.
8. **Durable writes:** if the lane is allowed to write lasting notes or memory outside its worktree, require the report to list every such write so you can sanity-check them.

## Per-harness cheat sheet

Verified on the author's fleet 2026-08-30; the `agy` row re-verified 2026-10-13. These CLIs drift fast - confirm any flag with `<cli> --help` on the current machine before scripting around it.

| CLI | Headless form | Model selection | Fleet notes |
|---|---|---|---|
| `claude` | `claude -p "prompt"` | `--model <id>` | `--output-format json` for parsing; each `-p` call is a full session on the metered pool |
| `codex` | `codex exec --cd <dir> - < prompt.md` | `-m <id>` | **Demoted below `opencode` for every OpenAI-subscription lane (read-only, review and write alike): use the `opencode` row by default, and reach for `codex exec` only when a sandbox is the point and the lane needs no TLS, ssh, or user-profile reads.** On Windows the `workspace-write` sandbox runs under a restricted token that cannot use the user's TLS credential store or read `~/.ssh` and `~/.config`: in a read-only lane on 2026-10-23 (with `network_access=true` and a `writable_roots` entry, banner reading "network access enabled") every authenticated curl to Gitea failed with `curl: (35) schannel: AcquireCredentialsHandle failed: SEC_E_NO_CREDENTIALS (0x8009030E)` and `git ls-remote` over ssh failed with `No ED25519 host key is known ... Host key verification failed`, so `writable_roots` and `network_access` do not cure it. Separately, `codex exec`'s `workspace-write` sandbox mounts `.git` read-only, even in a standalone (non-worktree) clone, so a lane can edit and run tests but cannot `git commit` (`fatal: Unable to create .../.git/index.lock: Read-only file system`, verified 2026-09-24) and the orchestrator must review and commit its diff by hand; if codex is used anyway, keep it to edit-only lanes whose diff the orchestrator commits; `-` reads the prompt from stdin (no shell quoting) and `--cd` sets the working directory - the cleanest dispatch form verified on this fleet; `codex exec` rejects `--full-auto` ("unexpected argument") since it is an interactive-mode flag - exec's defaults are already what a lane needs (approval `on-request`, sandbox `workspace-write` covering workdir, `/tmp`, `$TMPDIR`); pass `-s workspace-write` explicitly if you want it visible in the run banner; `--cd <dir>` outside a git repository fails with "Not inside a trusted directory and --skip-git-repo-check was not specified" - add `--skip-git-repo-check` for a scratch dir; to let a lane write its report outside the workdir, extend the sandbox with `-c 'sandbox_workspace_write.writable_roots=["C:/abs/path"]'` (the extra root shows in the banner's sandbox list), and for curl or git fetch inside the sandbox add `-c sandbox_workspace_write.network_access=true` (banner then reads "network access enabled"); on Windows the sandbox launches commands via CreateProcessAsUserW, and the WindowsApps `pwsh.exe` alias intermittently fails with Windows error 1920 ("The file cannot be accessed by the system") - the model retries and continues, so a few of these in a run log are noise, not a hang |
| `gemini` | `gemini -p "prompt"` | `gemini -m <id>` | print mode anchors relative paths wrong - absolute paths only |
| `agy` | `agy --model <id-with-effort-suffix> --mode accept-edits --add-dir <dir> --print "<prompt>" --print-timeout 25m` | `agy --model <id>` (include the effort suffix; verify with a probe that the model you asked for is the one answering) | model ids carry the reasoning effort as a suffix (`<model>-low\|medium\|high`), so the id alone selects model and effort - take ids from `agy models`, not from this table. `--effort low\|medium\|high\|xhigh\|max` still exists and can be paired with a suffix-less id; combining it with a suffixed id exits 1 with "conflicts with --effort" (use one or the other). The strongest model on this pool is a viable lane for demanding single-lane work (review, hard implementation) on a pool far less constrained than the Anthropic one; `--print` takes the prompt as its VALUE (Go-style flag parsing), not a trailing positional - putting `--model` after `--print` silently swallows it as the prompt and the session just answers with a greeting; headless file edits need `--mode accept-edits` (or the blanket `--dangerously-skip-permissions`) or the run hangs on an edit approval it cannot show; `--mode accept-edits` approves edits only - a lane that needs any other tool (a URL read, for example) has that tool auto-denied in headless mode and ends at once with exit 0 and the single line `no output produced - a tool required the "read_url" permission that headless mode cannot prompt for`, so a review or research lane needs an allow rule in agy's own settings or the blanket flag, and exit 0 with no report file is a failure; Claude Code's auto-mode classifier denies both auto-approve flags unless a user-approved `"Bash(agy*)"` prefix allow rule already exists in `~/.claude/settings.json` - do not work around a denial, surface it; runs entirely on its own pool, zero Anthropic quota used; `agy models` lists ids; 60-note batches hit the 25m `--print-timeout` roughly 1 run in 3 under 5-way concurrency (throttling, not content - same ranges succeed on retry) - recover by recomputing a remainder manifest from which files still lack the expected edit, not from git-dirty status, since checkpoint commits make dirty-lists lie; report files stay the reliable return channel, exit code 1 with "Error: timeout waiting for response" is the timeout signature |
| `opencode` | `opencode run "prompt"` | `-m provider/model` | Default lane for the OpenAI subscription for every lane kind (read-only, review and write), with `codex` demoted below it (`-m openai/<id>`: take the id from your tier mapping in the rules or memory layer, never from this table or from recall, and confirm it against `opencode models` - ids named in a skill go stale within weeks): same pool as codex, but no sandbox around `.git`, so a write lane commits its own work - pr-crew's OpenAI reviewer and writer lanes run this way; also a good home for local-model lanes (zero marginal cost) |
| `kimi` | `kimi -m <id> -p "prompt"` (the model flag MUST precede the prompt flag - `kimi -p <msg> -m <model>` wedged kimi 0.31.1 at startup) | `-m <id>` | `-p` is mutually exclusive with `--yolo` and `--auto` ("error: Cannot combine --prompt with --yolo") - plain `-p` already executes tool calls non-interactively (a 37-issue triage with git fetch, curl GET, and report-file writes completed under it); `--add-dir <path>` combines with `-p` to widen the writable workspace; run dispatches serially, one at a time; managed OAuth expiry needs an interactive login; each run prints a `kimi -r <session-id>` resume line at the end - capture it for verified follow-ups; a swarm-using lane goes SILENT on stdout and the parent shows no CPU while its subagents work (see hang detection below), so budget 30 to 45 minutes before judging it |
| `qwen` | `qwen -m <id> --approval-mode auto-edit -p "prompt"` | `-m <id>` (always explicit: the CLI can persist a model it cannot authenticate for) | For report-writing lanes, pass `--approval-mode auto-edit` explicitly: it enables file writes/edits while retaining the headless shell exclusion. Without an explicit mode, the headless mode comes from `tools.approvalMode` in `~/.qwen/settings.json`; with it unset, qwen 0.24 runs `auto`, whose tool list has no write, edit or shell tool. See the Qwen shell requirement below before enabling shell execution; stdout is fully buffered until completion, so a healthy run can be silent for its entire duration; `--output-format json` emits a single-line JSON array, `stream-json` emits JSONL; flag behavior drifts between versions |
| `pi` | see `pi --help` | `--model <id>` | Headless runs can hang before the first API call - see hang detection below |
| `muse` | `muse exec --prompt-file brief.md --approval-mode never --trust-workspace --reasoning-effort medium --no-session-log [--disable-write] [--max-model-steps N]` | `--model <id>` (the default in `~/.config/muse/settings.json` is the contributor tier; pass `--contributor-mode off` for anything not already public) | Meta Muse Code on Meta's own pool, zero Anthropic or OpenAI quota; provisional sonnet-tier (2026-10-11: three headless probes at medium effort, 28 to 53 s each, exact file:line citations, planted bugs all found, honest about a blocked test run). On Windows the elevated sandbox can block the shell even when `muse sandbox windows check` reports ready; follow the Muse shell requirement below before dispatch. `--disable-write` disables only non-shell workspace filesystem writes, not shell writes; `--output-schema` needs `additionalProperties: false` on every object or the API returns 400; plain stdout is the final answer and `--json` is a verbose JSONL event stream (dozens of lines for a one-word reply); it loads `~/.agents/skills` (catalog trimmed at 32 KB) and the workspace AGENTS.md (CLAUDE.md is ignored where AGENTS.md exists, AGENTS.md over 64 KB is truncated); no usage or quota command, so exhaustion behaviour is unknown - apply the Step 1 silent-hang rule |

**Muse shell requirement:** Keep shell filesystem/network sandboxing enabled and verify effective containment appropriate to the task before dispatching shell work on any platform; a Windows readiness check alone is insufficient. If containment cannot be established and verified, pass `--disable-shell` for non-shell work or route shell work to a sandboxed harness. Do not use `--disable-sandbox` or `--yolo` to work around blocked tests. `--trust-workspace` loads workspace rules and skills; it does not contain their effects. Neither `--disable-write` nor the Step 3 directory-scope prompt restricts shell writes. For read-only lanes, use both `--disable-write` and `--disable-shell`; have the driver capture Muse's stdout to the report file instead of asking Muse to write it.

**Qwen shell requirement:** If a lane needs shell execution, require an enforced sandbox with filesystem and process restrictions appropriate to the task, and verify those restrictions before dispatch. `--approval-mode yolo` auto-approves tool calls; it does not enable a sandbox. Do not use unsandboxed yolo to fix a missing report. Neither `auto-edit` nor the Step 3 directory-scope prompt is a containment boundary. If a sandbox cannot be established and verified, keep the lane report-only or route the shell work to a sandboxed harness. Confirm the installed version's flags and effective tool permissions before dispatch, and use a bounded report-write probe before a long run.

Where a fleet dispatch library exists (the author's is `llm_harness`, part of [schoen-lab](https://github.com/mtschoen/schoen-lab)), prefer it over hand-rolled subprocess calls: it already owns budgets, streaming, output parsing, and the quoting traps.

## Detecting a hung dispatch

A hung harness burns its whole budget silently and is indistinguishable from "slow" without process inspection. Before trusting any long-running dispatch, check:

- elapsed vs CPU time (`ps -o etime,time -p <pid>`): minutes elapsed with about zero CPU is a hang, not thinking;
- no child processes and no open TCP connections: it never reached the API;
- the report file (or log) has stopped growing - but ONLY as a secondary signal: some CLIs buffer stdout until completion (qwen among them), so a silent log with active children and accruing CPU is a healthy run, not a hang. A real 25-minute qwen dispatch on this fleet produced zero output while working and a correct report at the end; process inspection, not output growth, is the deciding signal.

Two harnesses defeat both signals at once, so check their own session records before killing:

- **kimi**: once the main agent dispatches an `AgentSwarm` or `Task` subagent, `-p` stdout stops (only the main agent's text streams) and the parent process accrues no CPU because the work is server-side LLM decoding (about 100 s per response at thinking effort max). Read `~/.kimi-code/sessions/wd_<repo>_<hash>/session_<id>/logs/kimi-code.log` (`llm request` / `llm response` lines with timestamps) or `agents/<agent>/wire.jsonl` (last event per agent); the session id is in `~/.kimi-code/session_index.jsonl` and `state.json` lists the swarm subagents. Fresh `llm.request` events within the last few minutes mean a healthy run. A 2026-10-26 MFTLib audit lane was killed after ten silent minutes on exactly this pattern; its subagents were mid-request at the kill.
- **qwen**: stdout is fully buffered until completion (above), so the log signal is void by design.

Kill it only when the harness's own record is also quiet; keep the partial log for diagnosis, and re-route the lane - do not wait it out.

## Interactive use

You cannot sit inside another CLI's TUI, so "interactive" diversion has exactly two workable shapes: the user drives that harness themselves (out of band, not orchestrated by you), or you approximate interactivity by re-invoking the headless mode with follow-up prompts - some CLIs support session resume or continue flags (check `--help`), which makes the follow-up carry prior context instead of restating it. Headless-with-report-file remains the proven default; treat resume-based follow-ups as an optimization to verify per CLI before relying on it.

## When not to route

- Judgment-heavy orchestration, final review, and delicate git or live-system steps stay in the orchestrator seat.
- Tasks that need this session's MCP servers, accumulated conversation context, or live user interaction.
- Trivial one-shot questions - dispatch overhead exceeds the savings.
- Anything built-in subagents already handle on a pool you are not trying to conserve.

## Common mistakes

| Mistake | What happens | Fix |
|---|---|---|
| Assuming a CLI is installed/authed | Dispatch fails minutes in, or hangs on a login prompt | `command -v` inventory + cheap auth probe (Step 1) |
| Hand-rolled inline prompts | Shell quoting mangles the brief; nothing to review | Prompt file on disk, driver reads it |
| Trusting stdout as the result | Bounded/truncated/lost output, silent failure | Report file at an absolute path, required by the prompt |
| Relative paths in the prompt | Agent edits files under its own launch dir | Absolute paths only |
| No missing-file clause | Agent improvises a plausible answer from thin context | "STOP and report BLOCKED naming it" in every prompt |
| Unscoped prompt | Lane edits live config or the wrong checkout | "Work ONLY in this directory" + a worktree for write tasks |
| Omitting the model flag | Lane inherits the orchestrator's premium tier | Explicit model on every dispatch |
| Farming volume to an "infinite" pool | Weekly cap hit; runs stall silently with no error | Verify caps first; on a silent run, confirm inactivity via hang detection (including session records), kill, and re-route |
| Waiting out a quiet run | Hung harness burns its whole budget | Process inspection (hang detection), kill, re-route |
