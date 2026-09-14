#!/usr/bin/env python3
"""Claude Code PreToolUse hook: mechanical enforcement of project locks.

Denies Edit/Write/NotebookEdit calls whose target is governed by another
session's cooperative lock, and denies unlocked writes with the exact acquire
command. Bash coverage is bounded: cwd jurisdiction plus absolute-path-token
scan. Fail-open by design: cooperative coordination must not brick the
harness on its own bugs.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

# aislop-ignore-next-line ai-slop/hallucinated-import -- sys.path-resolved sibling; stdlib-only
from project_lock.core import (
    MARKER_DIRECTORY_NAME,
    governing_lock,
    has_git_ancestor,
    is_under_temp_dir,
    nearest_worktree_root,
    read_json,
    state_directory,
    valid_metadata,
)

# aislop-ignore-next-line ai-slop/hallucinated-import -- sys.path-resolved sibling; stdlib-only
from project_lock.git_admin import (
    governing_checkout,
    is_git_admin_path,
    lock_at,
    repository_is_idle,
)

FILE_TOOL_PATH_KEYS = {"Edit": "file_path", "Write": "file_path", "NotebookEdit": "notebook_path"}
ALLOW = 0
DENY = 2
READ_ONLY_COMMANDS = frozenset(
    {"ls", "cat", "head", "tail", "grep", "rg", "pwd", "echo", "which", "type"}
)
POWERSHELL_READ_ONLY_COMMANDS = frozenset(
    {
        "get-content",
        "get-item",
        "get-childitem",
        "select-string",
        "test-path",
        "get-filehash",
        "get-command",
        "write-output",
    }
)
GIT_READ_SUBCOMMANDS = frozenset({"status", "log", "diff", "show", "ls-files", "rev-parse"})
FIND_MUTATING_FLAGS = frozenset(
    {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"}
)
SEGMENT_SPLIT_PATTERN = re.compile(r"[;&|\n\r]+")
PATH_TOKEN_PATTERN = re.compile(
    r"[A-Za-z]:[\\/][^\s'\"]+"
    r"|~[\\/][^\s'\"]+"
    r"|\$[A-Za-z_][A-Za-z0-9_]*[\\/][^\s'\"]+"
    r"|/[^\s'\"]+"
)


def enforcement_mode() -> str:
    return os.environ.get("PROJECT_LOCK_ENFORCE", "deny").strip().lower()


def lock_is_foreign(lock: dict | None, session_id: str) -> bool:
    if lock is None:
        return True
    session = lock.get("session")
    return not session or session != session_id


def describe_lock(governed: dict) -> str:
    lock = governed["lock"] or {}
    owner = lock.get("owner", "unknown")
    session = lock.get("session") or "unknown"
    reason = lock.get("reason", "metadata unavailable")
    expected = lock.get("expected_until", "unknown")
    return (
        f"project-lock: {governed['root']} is locked by {owner} (session {session})\n"
        f"  reason: {reason}\n  expected until: {expected}\n"
        "  Wait, work elsewhere, or use a separate git worktree. Overdue "
        "locks are not free; contact the owner before any force-clear."
    )


def acquire_recipe(target: str, session_id: str) -> str:
    root = nearest_worktree_root(target)
    script = Path(__file__).resolve().parents[1] / "scripts" / "project-lock.py"
    session_flag = f" --session {session_id}" if session_id else ""
    return (
        f"project-lock: no lock held for {root}\n"
        "  Acquire one before writing:\n"
        f'  python "{script}" acquire "{root}" --reason "<why>" '
        f"--duration 30m{session_flag}\n"
        "  Then retry this edit."
    )


def git_admin_refusal(target: str, checkout: Path, session_id: str) -> str:
    script = Path(__file__).resolve().parents[1] / "scripts" / "project-lock.py"
    session_flag = f" --session {session_id}" if session_id else ""
    return (
        f"project-lock: {target} is Git administration state\n"
        f"  It is shared by every worktree, and owned by the checkout at {checkout}.\n"
        "  Another worktree of this repository is locked, so claim that checkout\n"
        "  first and you are in charge of the repository:\n"
        f'  python "{script}" acquire "{checkout}" --reason "<why>" '
        f"--duration 30m{session_flag}\n"
        "  Then retry this edit."
    )


def check_git_admin(target: str, session_id: str) -> tuple[int, str]:
    """Authorize a write to Git administration state.

    Ownership rests with the governing checkout: holding its lock means being
    in charge of the repository and free to edit `.git` directly. This is what
    makes hand-editing possible for the cases Git offers no command for
    (`info/exclude`, `hooks/`, a `config` too malformed for `git config`).
    """
    checkout = governing_checkout(target)
    lock = lock_at(checkout)
    if lock is not None:
        if lock_is_foreign(lock, session_id):
            return DENY, describe_lock({"root": str(checkout), "lock": lock})
        return ALLOW, ""
    if repository_is_idle(checkout):
        # Nobody is working anywhere in this repository, so there is no one to
        # coordinate with and nothing to claim.
        return ALLOW, ""
    return DENY, git_admin_refusal(target, checkout, session_id)


def resolve_target(target: str, cwd: str | None) -> str:
    path = Path(target)
    if path.is_absolute():
        return target
    return str(Path(cwd or ".") / path)


def check_file_tool(payload: dict) -> tuple[int, str]:
    tool_input = payload.get("tool_input") or {}
    target = tool_input.get(FILE_TOOL_PATH_KEYS[payload["tool_name"]])
    if not target:
        return ALLOW, ""
    target = resolve_target(target, payload.get("cwd"))
    session_id = payload.get("session_id", "")
    # Resolved ahead of the ordinary lock check: this state is shared by every
    # worktree, so it answers to the governing checkout rather than to whatever
    # lock happens to sit above the path.
    if is_git_admin_path(target):
        return check_git_admin(target, session_id)
    governed = governing_lock(target)
    if governed is None:
        # No lock is registered anywhere above this path. Allow only true
        # scratch space: no Git checkout anywhere above the target AND the
        # target sits under a recognized system temp location ($TMPDIR,
        # /tmp, /private/tmp, /var/folders, tempfile.gettempdir()). Both
        # conditions matter -- a Git checkout under a temp mount (e.g. a
        # test fixture under /tmp) is still a real, coordinable project and
        # stays enforced; a non-Git project directory elsewhere (e.g.
        # ~/Documents/notes) also stays enforced, since agents still need to
        # coordinate writes there even without Git.
        if not has_git_ancestor(target) and is_under_temp_dir(target):
            return ALLOW, ""
        return DENY, acquire_recipe(target, session_id)
    if lock_is_foreign(governed["lock"], session_id):
        return DENY, describe_lock(governed)
    return ALLOW, ""


def segment_is_read_only(segment: str) -> bool:
    if (
        ">" in segment
        or "$(" in segment
        or "`" in segment
        or "--output" in segment
        or "<(" in segment
    ):
        return False
    words = segment.strip().split()
    if not words:
        return True
    first = words[0]
    if first.casefold() in POWERSHELL_READ_ONLY_COMMANDS:
        return not any(character in segment for character in "(){}")
    if first == "git":
        subcommand = next((word for word in words[1:] if not word.startswith("-")), "")
        return subcommand in GIT_READ_SUBCOMMANDS
    if first == "find":
        return not any(word in FIND_MUTATING_FLAGS for word in words[1:])
    return first in READ_ONLY_COMMANDS


def command_is_read_only(command: str) -> bool:
    return all(segment_is_read_only(segment) for segment in SEGMENT_SPLIT_PATTERN.split(command))


def registry_lock_roots() -> list[tuple[str, dict]]:
    registry_directory = state_directory() / "locks"
    if not registry_directory.exists():
        return []
    roots: list[tuple[str, dict]] = []
    for entry in registry_directory.glob("*.json"):
        metadata = valid_metadata(read_json(entry))
        if metadata is None:
            continue
        root = metadata.get("root", "")
        if root and (Path(root) / MARKER_DIRECTORY_NAME).exists():
            roots.append((root, metadata))
    return roots


def check_bash(payload: dict) -> tuple[int, str]:
    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command", "")
    session_id = payload.get("session_id", "")
    cwd = payload.get("cwd") or "."
    governed = governing_lock(cwd)
    if (
        governed is not None
        and lock_is_foreign(governed["lock"], session_id)
        and not command_is_read_only(command)
    ):
        return DENY, describe_lock(governed)
    if not command_is_read_only(command):
        for root, metadata in registry_lock_roots():
            if not lock_is_foreign(metadata, session_id):
                continue
            prefix = root.rstrip("\\/") + os.sep
            for token in PATH_TOKEN_PATTERN.findall(command):
                expanded = os.path.expandvars(os.path.expanduser(token))
                normalized = os.path.normcase(os.path.normpath(expanded))
                if normalized == root or normalized.startswith(prefix):
                    return DENY, describe_lock({"root": root, "lock": metadata})
    return ALLOW, ""


def evaluate(payload: dict) -> tuple[int, str]:
    tool_name = payload.get("tool_name", "")
    if tool_name in FILE_TOOL_PATH_KEYS:
        return check_file_tool(payload)
    if tool_name == "Bash":
        return check_bash(payload)
    return ALLOW, ""


def main() -> int:
    mode = enforcement_mode()
    if mode == "off":
        return ALLOW
    try:
        payload = json.load(sys.stdin)
        decision, message = evaluate(payload)
    except Exception as error:  # fail-open by design
        print(f"project-lock hook error (allowing): {error}", file=sys.stderr)
        return ALLOW
    if message:
        print(message, file=sys.stderr)
    if mode == "warn":
        return ALLOW
    return decision


if __name__ == "__main__":
    sys.exit(main())
