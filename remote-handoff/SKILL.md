---
name: remote-handoff
description: Use when the user is about to leave the machine but wants to start or continue agent work from their phone or another device later - "kick off the next session remotely", "I'm heading out, leave a session I can drive from my phone", "can I start this from claude.ai", or at the end of a wrap when the follow-up session must run on THIS machine's hardware (a device-attached test rig, a GPU box, a Steam Deck). Leaves a fresh, remotely controllable interactive session idle in tmux, hands the user its URL, and records how to reattach locally.
---

# remote-handoff

## Overview

Claude Code's Remote Control lets a phone or browser drive a session that is
**already running on the local machine**. Nothing starts a local session from
outside: routines and cloud sessions run on Anthropic infrastructure, and
Dispatch needs the Desktop app already running. So when work has to happen on
this machine's hardware and the user is walking away, the agent's job is to
leave the right kind of session behind and tell the user how to find it.

Verified 2026-09-06 (Claude Code 2.1.263, SteamOS): the recipe below produced a
session that appeared in the mobile app's session list, was driven from the
phone, and was later attached locally with `tmux attach`.

## Mode selection: interactive session vs. server mode

| Command | Primary role | What the client sees | Recommended when |
| --- | --- | --- | --- |
| `claude` then `/remote-control` inside it | Direct interactive session | A specific, active **session** in the session list with a direct URL | Handing off a specific prepared session with verified readiness |
| `claude remote-control` | Persistent environment manager ("spawn mode") | A remote **environment** capable of spawning sessions (defaults `createSessionInDir: true`) | Keeping the machine available to spin up multiple sessions on demand |

### Why interactive mode is the default for single-session handoffs

In server mode (`claude remote-control`), the CLI runs an environment manager
and attempts to create an initial bridge session in the working directory
(`createSessionInDir` defaults to true on startup, unless disabled via
`--no-create-session-in-dir`). However, server mode catches session-creation
errors without exiting the server process. In incident testing (2026-09-06),
when bridge session creation failed during startup, the environment registered
successfully while the mobile session list remained empty, leaving the user
unable to find or drive the expected session.

Furthermore, server mode hosts multiple concurrent sessions, so stopping it
interrupts all currently served sessions. Starting an interactive session in
tmux and enabling `/remote-control` explicitly initializes the terminal,
exposes the exact session URL upfront, and ensures the session is ready and
driveable before the operator departs.

## Recipe

1. Finish the current session's own closing work first: commit or push what
   should be durable, release the project lock (the new session takes its
   own), and write the kickoff prompt into the handoff note so the user can
   paste it from the phone.

2. Start an interactive session in a detached tmux session, in the repo and on
   the branch the next session should use:

   ```bash
   tmux new-session -d -s <name> -c /path/to/repo 'claude'
   ```

   tmux keeps the process alive after the agent's own shell exits and lets the
   user attach later. Give the tmux session a name that says what it is for.

3. Wait for the prompt, then enable remote control from inside:

   ```bash
   tmux send-keys -t <name> "/remote-control" Enter
   sleep 15
   tmux capture-pane -pt <name> | grep -A3 "Remote Control"
   ```

   The dialog prints the session URL
   (`https://claude.ai/code/session_...`). Capture it, then send `Enter` once
   more to dismiss the dialog and leave the session idle at its prompt.

4. Hand the user three things, verbatim: the session URL, the kickoff prompt
   to paste, and the local reattach commands:

   ```bash
   tmux attach -t <name>            # live terminal; Ctrl+B then D detaches
   claude --resume <session id>     # same session in a plain terminal
   ```

5. If the user reports the session is missing from the list, inspect
   `tmux capture-pane -pt <name>` before taking any action. Seeing
   `Connected · <repo>` with `Capacity: M/N` and an `environment=` URL confirms
   server mode (`claude remote-control`), but recognizing server mode does not
   establish that session creation failed or that stopping the server is safe.
   Work through this diagnosis:

   - **Check creation settings:** Verify how the process was started. If
     `--no-create-session-in-dir` was passed, initial session creation was
     disabled intentionally. The server is acting as an on-demand environment
     manager; the user should open the `environment=` URL or create a session
     from the mobile app's environment view rather than stopping the server.
   - **Check active sessions and blast radius:** Check `Capacity: M/N` and the
     session listing in the pane. Stopping server mode (`Ctrl+C`) disconnects
     every session the server is currently hosting. If `M > 0`, do NOT stop
     the server without confirming that interrupting those active sessions is
     acceptable.
   - **Diagnose creation errors:** If initial session creation was expected
     (the default `createSessionInDir: true`) but no session appears in the list:
     - Check the pane scrollback (`tmux capture-pane -S -100 -pt <name>`) or the
       debug file (if `--debug-file` was set) for non-fatal startup warnings:
       `[bridge:init] Session creation failed (non-fatal): <reason>` or
       `session pre-creation failed (non-fatal)`.
     - Check for authentication, network, or policy failures (for example,
       expired login credentials or organization policy restrictions).
   - **Choose the recovery path:**
     - *If server mode was intended:* If `--no-create-session-in-dir` was used,
       connect via the environment URL or mobile environment view. If session
       creation failed due to a transient error, wait for any active sessions
       to complete, stop the server (`Ctrl+C`), resolve the error (such as
       running `claude /login`), and restart `claude remote-control` (ensuring
       `--no-create-session-in-dir` is omitted). To reattach to a previously
       recorded session instead of creating a new one, use
       `claude remote-control --continue` or `--session-id <id>`.
     - *If server mode was started mistakenly:* Confirm that no other sessions
       are active (`Capacity: 0/N`), stop the server process (`Ctrl+C`), kill
       the tmux session if needed, and run the interactive session recipe
       (steps 2 and 3) to launch a direct, verified session.

## Gotchas

- In server mode the `w` key toggles between same-dir and worktree spawn
  modes; it does not turn spawn mode off or convert the process into an
  interactive CLI session.
- Server mode startup defaults `createSessionInDir` to true and calls
  `createBridgeSession`, but catches session-creation failures without
  terminating the server. If session creation fails silently, the server
  stays connected while the mobile session list appears empty. If started
  with `--no-create-session-in-dir`, pre-creation is skipped intentionally.
- Stopping server mode (`Ctrl+C`) immediately disconnects all sessions it is
  serving. Before stopping a server to recover from a missing session, always
  verify `Capacity: M/N` in `tmux capture-pane` so running sessions are not
  aborted.
- `claude --version` and `claude remote-control --help` can hang for minutes
  in a non-interactive shell; do not put them in the critical path.
- An interactive session with remote control retries through network outages
  and only disconnects after about 30 minutes of failed presence heartbeats.
  Server mode exits after about 10 minutes unreachable. Idle time alone is not
  a limit on either.
- The new session starts with no conversation context. Everything it needs
  must be in the repo, the memory notes, or the kickoff prompt; that is why
  step 1 comes first.
- The session inherits whatever permission mode the terminal defaults to.
  If the user will drive it unattended, make sure that mode is the one they
  expect before walking away (the status line shows it).

## Sources

- [Remote Control docs](https://code.claude.com/docs/en/remote-control)
- [Mobile app docs](https://code.claude.com/docs/en/mobile)
