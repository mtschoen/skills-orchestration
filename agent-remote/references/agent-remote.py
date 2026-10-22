#!/usr/bin/env python3
"""
agent-remote.py - spawn an agent session (claude, opencode, agy, pi, or codex) on a remote host
to do work in an isolated git worktree, capture a structured result, return.
Kept as one standalone script so remote invocations need no package installation.

The "open a terminal" affordance for agent orchestrators: instead of piping
each command over ssh, hand a task to a remote agent session running
in its own warm shell with persistent context.

Usage:
    agent-remote.py run \
        --host user@remote-host \
        --repo-path ~/myrepo \
        --prompt "Build nvbandwidth, run it against all GPUs, report the matrix." \
        [--branch agent-remote/nvbw-2026-04-07] \
        [--permission-mode acceptEdits] \
        [--agent opencode] \
        [--model ollama/<model-id>] \
        [--os auto|windows|posix]

    agent-remote.py cleanup \
        --host user@remote-host \
        --branch agent-remote/nvbw-2026-04-07 \
        --repo-path ~/myrepo \
        [--os auto|windows|posix]

    agent-remote.py probe \
        --host user@remote-host \
        --repo-path ~/myrepo \
        [--os auto|windows|posix]

`run` returns a JSON result on stdout with:
    host, branch, worktree_path, parent_commit, new_commit (or null),
    files_changed, agent_exit_code, stdout_tail, stderr_tail,
    cleanup_command

Environment variables:
    REMOTE_AGENT_ALLOW_BYPASS=1   Allow --permission-mode bypassPermissions
                                  (otherwise that mode is refused)
    REMOTE_AGENT_TIMEOUT=3600     Max seconds for the remote agent run
                                  (default 3600)
    REMOTE_AGENT_OS=windows|posix Force remote OS type
"""

from __future__ import annotations

import argparse
import base64
import json
import ntpath
import os
import posixpath
import re

# CRITICAL: must run before any subprocess imports/usage so the child env
# inherits the override. On Windows + Git Bash / MSYS, ssh.exe's argv is
# preprocessed to convert any /foo/bar argument into C:/Program Files/Git/foo/bar,
# mangling every absolute remote path. Setting these env vars on os.environ
# (rather than just in subprocess.run's env=) ensures the override sticks
# across the Python -> ssh.exe boundary regardless of how subprocess builds
# the child env block on Windows.
os.environ.setdefault("MSYS_NO_PATHCONV", "1")
os.environ.setdefault("MSYS2_ARG_CONV_EXCL", "*")

import secrets
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field

# Written into the remote worktree's .claude/settings.local.json before claude -p.
# Noninteractive permission prompts become denials; callers can add tools through
# --extra-allow without enabling bypassPermissions. Bash itself is unrestricted.
DEFAULT_REMOTE_ALLOWLIST: list[str] = [
    "Bash",  # full Bash - trust model assumes `host` is the user's own
    # machine. The worktree isolates the checkout/branch, not the OS: it does
    # not sandbox the filesystem, so this Bash entry can still read ~/.ssh,
    # exfiltrate data, or rm -rf anything the ssh user can reach. Don't point
    # this at a host you don't already trust with full shell access. To
    # narrow it, edit this list (e.g. swap "Bash" for specific
    # `Bash(cmd *)` patterns) - there's no CLI flag to shrink it, only
    # --extra-allow to widen it.
    "Read",
    "Edit",
    "Write",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
]


@dataclass
class RunResult:
    host: str
    branch: str
    worktree_path: str
    parent_commit: str
    new_commit: str | None
    files_changed: list[str]
    agent_exit_code: int
    stdout_tail: str
    stderr_tail: str
    cleanup_command: str
    success: bool = field(init=False)

    def __post_init__(self) -> None:
        self.success = self.agent_exit_code == 0

    def to_json(self) -> str:
        payload = {
            "success": self.success,
            "host": self.host,
            "branch": self.branch,
            "worktree_path": self.worktree_path,
            "parent_commit": self.parent_commit,
            "new_commit": self.new_commit,
            "files_changed": self.files_changed,
            "agent_exit_code": self.agent_exit_code,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
            "cleanup_command": self.cleanup_command,
        }
        return json.dumps(payload, indent=2)


#: PATH prefix injected before every remote POSIX command. Ensures user-local
#: install dirs (~/.local/bin, ~/.npm-global/bin, ~/bin) are reachable
#: from non-interactive ssh sessions, where many distros' login shells
#: leave them off PATH. Without this, `claude`, `opencode`, `agy`, `pipx`-installed
#: tools, and a lot of npm-global binaries are mysteriously "not found."
REMOTE_PATH_PREFIX = "$HOME/.local/bin:$HOME/.npm-global/bin:$HOME/bin"

#: PowerShell expression whose output is `Win32NT` on Windows hosts.
WINDOWS_PLATFORM_EXPRESSION = "[Environment]::OSVersion.Platform"

#: Number of random bytes behind the token in a bootstrap's temporary file name.
BOOTSTRAP_TOKEN_BYTES = 8

#: In-memory cache of detected remote host OS types ('windows' or 'posix').
_HOST_OS_CACHE: dict[str, str] = {}


def clear_host_os_cache() -> None:
    """Clear cached OS detection results."""
    _HOST_OS_CACHE.clear()


def set_host_os(host: str, os_type: str) -> None:
    """Explicitly record the OS type for a host."""
    _HOST_OS_CACHE[host] = os_type


def get_cached_host_os(host: str) -> str | None:
    """Return the cached OS type for a host if known."""
    return _HOST_OS_CACHE.get(host)


def ssh_raw_run(
    host: str,
    raw_command: str,
    *,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    """
    Run an unwrapped command directly over ssh without any shell wrapping.
    Used for OS detection probes before shell wrapping is chosen.
    """
    argv = ["ssh", "-o", "BatchMode=yes", host, raw_command]
    env = {**os.environ, "MSYS_NO_PATHCONV": "1", "MSYS2_ARG_CONV_EXCL": "*"}
    run_kwargs: dict = {
        "capture_output": True,
        "text": True,
        "timeout": timeout,
        "env": env,
        "encoding": "utf-8",
        "errors": "replace",
        "stdin": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(argv, **run_kwargs, check=False)


def detect_remote_os(host: str, *, os_override: str | None = None) -> str:
    """
    Detect whether the remote host runs Windows (native cmd.exe/PowerShell)
    or POSIX (Linux/macOS/BSD).

    Checks in order:
      1. Explicit os_override argument if not 'auto' or None
      2. In-memory cache (_HOST_OS_CACHE)
      3. REMOTE_AGENT_OS environment variable ('windows' or 'posix')
      4. Probe via unwrapped ssh running `ver`
    """
    if os_override and os_override != "auto":
        _HOST_OS_CACHE[host] = os_override
        return os_override

    if host in _HOST_OS_CACHE:
        return _HOST_OS_CACHE[host]

    env_os = os.environ.get("REMOTE_AGENT_OS")
    if env_os in ("windows", "posix"):
        _HOST_OS_CACHE[host] = env_os
        return env_os

    # Run unwrapped OS probe over ssh.
    # Probe 1: Check login shell for POSIX (Linux, macOS, BSD, WSL).
    # Running `uname -s` directly under the login shell ensures WSL
    # (which reports 'Linux') is affirmatively identified as POSIX without
    # launching cmd.exe or triggering Windows interop.
    result = ssh_raw_run(host, "uname -s", timeout=15)
    if result.returncode == 255:
        raise RuntimeError(
            f"SSH connection failed while detecting OS on {host}: "
            f"exit 255\nstderr: {result.stderr.strip()}"
        )
    stdout_posix = result.stdout.strip().lower()
    if result.returncode == 0 and stdout_posix in (
        "linux",
        "darwin",
        "freebsd",
        "openbsd",
        "netbsd",
        "sunos",
        "aix",
    ):
        _HOST_OS_CACHE[host] = "posix"
        return "posix"

    # Probe 2: If not affirmative POSIX, probe for Windows.
    # Uses PowerShell to detect Windows independently of cmd.exe built-ins,
    # working under cmd.exe, PowerShell and Git Bash OpenSSH login shells.
    # The inline expression has no quotes, spaces, or $ signs, so no outer
    # shell expands or splits it.
    win_result = ssh_raw_run(
        host,
        f"powershell -NoProfile -NonInteractive -Command {WINDOWS_PLATFORM_EXPRESSION}",
        timeout=15,
    )
    if win_result.returncode == 255:
        raise RuntimeError(
            f"SSH connection failed while detecting OS on {host}: "
            f"exit 255\nstderr: {win_result.stderr.strip()}"
        )
    win_stdout = win_result.stdout.strip().lower()
    if win_result.returncode == 0 and "win32nt" in win_stdout:
        _HOST_OS_CACHE[host] = "windows"
        return "windows"

    # Probe 3 (fallback): Check `ver` for cmd-only environments without PowerShell.
    ver_result = ssh_raw_run(host, "ver", timeout=15)
    if ver_result.returncode == 255:
        raise RuntimeError(
            f"SSH connection failed while detecting OS on {host}: "
            f"exit 255\nstderr: {ver_result.stderr.strip()}"
        )
    ver_stdout = ver_result.stdout.strip().lower()
    if ver_result.returncode == 0 and "windows" in ver_stdout:
        _HOST_OS_CACHE[host] = "windows"
        return "windows"

    # Neither affirmative POSIX nor affirmative Windows: do not cache.
    err_detail = (
        f"uname exit={result.returncode} stdout={result.stdout.strip()!r}; "
        f"win probe exit={win_result.returncode} stdout={win_result.stdout.strip()!r}; "
        f"ver probe exit={ver_result.returncode} stdout={ver_result.stdout.strip()!r}"
    )
    raise RuntimeError(
        f"Could not determine remote OS on {host}: inconclusive probe results ({err_detail})"
    )


def rpath(p: str) -> str:
    """
    Convert a POSIX absolute path so it survives MSYS argv processing on
    Windows. MSYS-built executables (Git Bash's ssh.exe, git.exe) auto-
    convert any /foo/bar argument into C:/Program Files/Git/foo/bar. They
    skip paths starting with `//`. POSIX systems collapse leading `//` to
    `/`, so on the REMOTE side `//home/x` and `/home/x` are the same path.

    Setting MSYS_NO_PATHCONV=1 / MSYS2_ARG_CONV_EXCL=* in the env does NOT
    reliably catch this when the absolute path is embedded inside a string
    arg passed to ssh - only path-shaped argv elements are guarded. The
    only robust workaround is to mangle the path itself.

    No-op on Linux/macOS.
    """
    if sys.platform == "win32" and p.startswith("/") and not p.startswith("//"):
        return "/" + p
    return p


def qrp(p: str, *, os_type: str = "posix") -> str:
    """
    Quote a remote path for embedding in a shell command.
    On POSIX, applies MSYS-safe path mangling and shlex.quote.
    On Windows, wraps in double quotes for cmd.exe.
    """
    if os_type == "windows":
        escaped = p.replace('"', '\\"')
        return f'"{escaped}"'
    return shlex.quote(rpath(p))


#: Common Git for Windows install prefixes that MSYS prepends to converted
#: POSIX paths. Order matters; check longest first.
_MSYS_PREFIXES = [
    "C:/Program Files/Git",
    "C:\\Program Files\\Git",
    "C:/Program Files (x86)/Git",
    "C:\\Program Files (x86)\\Git",
]


def unmangle_msys_path(p: str) -> str:
    """
    Reverse Git Bash's outbound argv path mangling. When a user runs
    `python agent-remote.py --repo-path /home/user/myrepo` from Git
    Bash on Windows, MSYS rewrites `/home/user/myrepo` into
    `C:/Program Files/Git/home/user/myrepo` BEFORE python.exe sees
    its argv. Python has no way to recover the original string from its
    own environment, so we detect the well-known Git install prefix and
    strip it.

    Workarounds the user can also use:
      - export MSYS_NO_PATHCONV=1 in the shell, or
      - pass paths with a leading double-slash (//home/user/myrepo),
        which MSYS leaves alone.

    No-op on non-Windows or paths that don't look mangled.
    """
    if sys.platform != "win32":
        return p
    for prefix in _MSYS_PREFIXES:
        if p.startswith((prefix + "/", prefix + "\\")):
            # The stripped remainder starts with /, which is what we want for
            # a POSIX path.
            return p[len(prefix) :].replace("\\", "/")
    return p


def ssh_run(
    host: str,
    remote_command: str,
    *,
    input_text: str | None = None,
    timeout: float | None = None,
    os_type: str | None = None,
) -> subprocess.CompletedProcess:
    """
    Run a single command on the remote host via ssh.

    For POSIX remotes:
      Uses `bash -lc` so the remote PATH picks up login-shell additions
      (/opt/cuda/bin, conda, etc.) and injects REMOTE_PATH_PREFIX so
      user-local install dirs are reachable.

    For Windows remotes:
      Runs directly through the native shell (cmd.exe) without bash wrapping,
      preventing accidental redirection into WSL.
    """
    if os_type is None:
        os_type = detect_remote_os(host)

    if os_type == "windows":
        wrapped = remote_command
    else:
        # Prepend user-local install dirs, then run inside login shell.
        extended = f'export PATH="{REMOTE_PATH_PREFIX}:$PATH"; {remote_command}'
        wrapped = f"bash -lc {shlex.quote(extended)}"

    argv = ["ssh", "-o", "BatchMode=yes", host, wrapped]

    # On Windows + Git Bash / MSYS, ssh.exe's argv is preprocessed to
    # convert any `/foo/bar` arguments into `C:/Program Files/Git/foo/bar`,
    # which mangles every absolute remote path. Suppress this with
    # MSYS_NO_PATHCONV=1. No effect on real Linux/macOS.
    env = {**os.environ, "MSYS_NO_PATHCONV": "1", "MSYS2_ARG_CONV_EXCL": "*"}

    # When no input_text is provided, explicitly close child stdin. Without
    # this, ssh inherits the orchestrator's stdin and can hang waiting for
    # input in non-TTY contexts (e.g. when called from a subagent or
    # background task).
    run_kwargs: dict = {
        "capture_output": True,
        "text": True,
        "timeout": timeout,
        "env": env,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if input_text is not None:
        run_kwargs["input"] = input_text
    else:
        run_kwargs["stdin"] = subprocess.DEVNULL

    # On Windows, ssh.exe launched from Python's subprocess can hang
    # indefinitely on capture_output if it inherits the parent's console
    # window. CREATE_NO_WINDOW prevents the inheritance and lets ssh.exe
    # exit cleanly when its work is done. No effect on Linux/macOS.
    if sys.platform == "win32":
        run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    return subprocess.run(argv, **run_kwargs, check=False)


def scp_upload(
    host: str,
    local_path: str,
    remote_name: str,
    *,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    """Copy a local file to `remote_name` (relative to the remote home directory)."""
    argv = ["scp", "-q", "-o", "BatchMode=yes", local_path, f"{host}:{remote_name}"]
    env = {**os.environ, "MSYS_NO_PATHCONV": "1", "MSYS2_ARG_CONV_EXCL": "*"}
    run_kwargs: dict = {
        "capture_output": True,
        "text": True,
        "timeout": timeout,
        "env": env,
        "encoding": "utf-8",
        "errors": "replace",
        "stdin": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(argv, **run_kwargs, check=False)


def ps_literal(value: str) -> str:
    """Render `value` as a single-quoted PowerShell string literal."""
    escaped = re.sub(r"['\u2018\u2019\u201a\u201b]", r"\g<0>\g<0>", value)
    return f"'{escaped}'"


def build_powershell_bootstrap(token: str) -> str:
    """
    Build the remote command that runs a PowerShell script read from stdin.

    The command reads all of stdin as UTF-8, writes it to a temporary .ps1 file
    named after `token` (with a byte order mark so Windows PowerShell reads it
    as UTF-8), runs that file, deletes it, and exits with the script's exit
    code. The command line carries no script text and no encoded data.

    The text contains no dollar sign and no double quote, so the same command
    survives bash (which expands dollar signs inside double quotes), cmd.exe
    and PowerShell login shells unchanged.
    """
    if not token or any(character not in "0123456789abcdef" for character in token):
        raise ValueError("bootstrap token must be lowercase hex")
    script_path = f"(Join-Path ([IO.Path]::GetTempPath()) 'agent-remote-{token}.ps1')"
    statements = [
        f"Set-Variable script_path {script_path}",
        "[Console]::InputEncoding=[Text.UTF8Encoding]::new(0)",
        (
            "try { [IO.File]::WriteAllText((Get-Variable script_path -ValueOnly), [Console]::In.ReadToEnd(), "
            "[Text.UTF8Encoding]::new(1)); & (Get-Variable script_path -ValueOnly) } catch { throw } "
            "finally { Remove-Item -LiteralPath (Get-Variable script_path -ValueOnly) -Force -ErrorAction SilentlyContinue }"
        ),
        "exit (Get-Variable LASTEXITCODE -ValueOnly -ErrorAction Ignore)",
    ]
    bootstrap = "; ".join(statements)
    if "$" in bootstrap or '"' in bootstrap:
        raise ValueError("bootstrap must not contain a dollar sign or double quote")
    return f'powershell -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "{bootstrap}"'


def build_powershell_cleanup(
    token: str,
    *,
    include_payload: bool = True,
) -> str:
    """Attempt every artifact and report lookup/removal errors, but allow absence."""
    script_temp_name = f"agent-remote-{token}.ps1"
    paths = []
    if include_payload:
        paths.append(f"agent-remote-{token}.dat")
    paths.append(f"(Join-Path ([IO.Path]::GetTempPath()) '{script_temp_name}')")
    # Keep the aggregate status dollar-free for every supported login shell.
    statements = ["Set-Variable cleanup_status 0"]
    for path in paths:
        statements.append(
            f"try {{ if (Test-Path -LiteralPath {path} -ErrorAction Stop) {{ "
            f"Remove-Item -LiteralPath {path} -Force -ErrorAction Stop }} }} "
            "catch { [Console]::Error.WriteLine((Get-Variable Error -ValueOnly)[0]); "
            "Set-Variable cleanup_status 1 }"
        )
    statements.append("exit (Get-Variable cleanup_status -ValueOnly)")
    return (
        "powershell -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "
        f'"{"; ".join(statements)}"'
    )


def cleanup_remote_powershell_files(
    host: str,
    token: str,
    *,
    include_payload: bool = True,
    timeout: float | None = None,
) -> str | None:
    """Attempt removing staged PowerShell artifacts; return a warning on failure."""
    cleanup_command = build_powershell_cleanup(token, include_payload=include_payload)
    try:
        cleanup_result = ssh_run(
            host, cleanup_command, timeout=timeout, os_type="windows"
        )
        if cleanup_result.returncode != 0:
            detail = (
                cleanup_result.stderr.strip()
                if cleanup_result.stderr and cleanup_result.stderr.strip()
                else f"exit status {cleanup_result.returncode}"
            )
            return f"warning: could not remove remote files: {detail}"
        return None
    except (OSError, subprocess.SubprocessError) as cleanup_exc:
        return f"warning: could not remove remote files: {cleanup_exc}"


def ssh_run_powershell(
    host: str,
    script: str,
    *,
    payload: bytes | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    """
    Run a PowerShell script on a Windows remote through one ssh connection.

    The script travels on ssh's stdin as UTF-8 and a short bootstrap command
    (see `build_powershell_bootstrap`) saves it to a temporary file, runs it,
    and deletes it. Nothing is encoded or decoded on the command line, so
    cmd.exe, PowerShell and Git Bash login shells all behave the same, and
    the script can be any size.

    The script's `exit N` becomes ssh's exit status under cmd.exe and Git Bash
    login shells. Under a PowerShell login shell the status collapses to 0 or
    1, so a caller that needs the precise code should have the script print it.

    `payload`, when given, is copied with scp to the remote home directory and
    exposed to the script as `$payloadPath`. The script removes the payload
    when it finishes; if the run fails, a follow-up command removes it again
    in case the script never started (for example after a parse error).
    """
    token = secrets.token_hex(BOOTSTRAP_TOKEN_BYTES)
    payload_name = f"agent-remote-{token}.dat"
    payload_binding = ""
    wrapped = script
    if payload is not None:
        payload_binding = (
            f"$payloadPath = Join-Path (Get-Location) {ps_literal(payload_name)}\n"
        )
        wrapped = (
            f"{payload_binding}"
            "try {\n"
            f"{script}\n"
            "} finally {\n"
            "    Remove-Item -LiteralPath $payloadPath -Force -ErrorAction SilentlyContinue\n"
            "}\n"
        )

    def _cleanup_remote_files() -> str | None:
        return cleanup_remote_powershell_files(
            host,
            token,
            include_payload=payload is not None,
            timeout=timeout,
        )

    result: subprocess.CompletedProcess
    if payload is None:
        try:
            result = ssh_run(
                host,
                build_powershell_bootstrap(token),
                input_text=wrapped,
                timeout=timeout,
                os_type="windows",
            )
        except (OSError, subprocess.SubprocessError):
            cleanup_warning = _cleanup_remote_files()
            if cleanup_warning:
                print(cleanup_warning, file=sys.stderr)
            raise
    else:
        with tempfile.TemporaryDirectory() as staging:
            payload_file = os.path.join(staging, payload_name)
            with open(payload_file, "wb") as handle:
                handle.write(payload)
            try:
                result = scp_upload(host, payload_file, payload_name, timeout=timeout)
                if result.returncode == 0:
                    result = ssh_run(
                        host,
                        build_powershell_bootstrap(token),
                        input_text=wrapped,
                        timeout=timeout,
                        os_type="windows",
                    )
            except (OSError, subprocess.SubprocessError):
                cleanup_warning = _cleanup_remote_files()
                if cleanup_warning:
                    print(cleanup_warning, file=sys.stderr)
                raise

    if result.returncode != 0:
        cleanup_warning = _cleanup_remote_files()
        if cleanup_warning:
            result_stderr = result.stderr or ""
            result.stderr = "\n".join(
                part for part in (result_stderr, cleanup_warning) if part
            )
    return result


def ssh_put_file(
    host: str,
    remote_path: str,
    content: str,
    *,
    os_type: str | None = None,
) -> None:
    """
    Write `content` to `remote_path` on the remote host.

    On POSIX remotes, uses stdin redirection with `cat >`.
    On Windows remotes, copies the content with scp and moves it into place
    from a PowerShell script sent over ssh stdin, creating parent directories
    on the way.
    """
    if os_type is None:
        os_type = detect_remote_os(host)

    if os_type == "windows":
        ps_script = (
            "$ErrorActionPreference = 'Stop'\n"
            f"$path = [System.IO.Path]::GetFullPath({ps_literal(remote_path)})\n"
            "$parent = [System.IO.Path]::GetDirectoryName($path)\n"
            "if ($parent -and -not (Test-Path -LiteralPath $parent)) {\n"
            "    New-Item -ItemType Directory -Path $parent -Force | Out-Null\n"
            "}\n"
            "if (Test-Path -LiteralPath $path -PathType Container) {\n"
            "    throw 'Destination path is an existing directory: ' + $path\n"
            "}\n"
            "Move-Item -LiteralPath $payloadPath -Destination $path -Force"
        )
        result = ssh_run_powershell(host, ps_script, payload=content.encode("utf-8"))
    else:
        parent = posixpath.dirname(remote_path)
        cmd = f"mkdir -p {qrp(parent, os_type=os_type)} && cat > {qrp(remote_path, os_type=os_type)}"
        result = ssh_run(host, cmd, input_text=content, os_type=os_type)

    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to write {remote_path} on {host}: "
            f"exit {result.returncode}, stderr: {result.stderr}"
        )


def ssh_check(
    host: str,
    remote_command: str,
    *,
    error_context: str = "",
    os_type: str | None = None,
) -> str:
    """Run a command, raise if it fails, return stdout."""
    result = ssh_run(host, remote_command, os_type=os_type)
    if result.returncode != 0:
        ctx = f" ({error_context})" if error_context else ""
        raise RuntimeError(
            f"Remote command failed{ctx} on {host}: "
            f"`{remote_command}`\nexit {result.returncode}\n"
            f"stderr: {result.stderr}"
        )
    return result.stdout


def compute_worktree_path(
    repo_path: str,
    branch: str,
    *,
    os_type: str = "posix",
) -> str:
    """
    Worktrees live SIBLING to the repo, not inside it, under an
    `agent-remote-worktrees/` directory. Branch-name is used as the
    directory name, with slashes replaced.
    """
    safe_branch = branch.replace("/", "_")
    if os_type == "windows":
        safe_branch = safe_branch.replace("\\", "_")
        if "\\" in repo_path and "/" not in repo_path:
            parent = ntpath.dirname(repo_path.rstrip("\\"))
            return f"{parent}\\agent-remote-worktrees\\{safe_branch}"
        cleaned = repo_path.replace("\\", "/").rstrip("/")
        parent = posixpath.dirname(cleaned)
        return f"{parent}/agent-remote-worktrees/{safe_branch}"

    parent = posixpath.dirname(repo_path.rstrip("/"))
    return f"{parent}/agent-remote-worktrees/{safe_branch}"


def ensure_worktree(
    host: str,
    repo_path: str,
    branch: str,
    *,
    os_type: str | None = None,
) -> tuple[str, str]:
    """
    Create or reuse a git worktree for `branch` on the remote.
    Returns (worktree_path, parent_commit_sha).
    """
    if os_type is None:
        os_type = detect_remote_os(host)

    worktree_path = compute_worktree_path(repo_path, branch, os_type=os_type)

    quoted_repo = qrp(repo_path, os_type=os_type)
    existing = ssh_check(
        host,
        f"git -C {quoted_repo} worktree list --porcelain",
        error_context="list worktrees",
        os_type=os_type,
    )
    for block in existing.strip().split("\n\n"):
        if f"branch refs/heads/{branch}" in block:
            # Worktree already exists - reuse it.
            wt_line = next(
                line for line in block.splitlines() if line.startswith("worktree ")
            )
            worktree_path = wt_line.split(" ", 1)[1]
            quoted_wt = qrp(worktree_path, os_type=os_type)
            parent = ssh_check(
                host,
                f"git -C {quoted_wt} rev-parse HEAD",
                os_type=os_type,
            ).strip()
            return worktree_path, parent

    # Fresh worktree: create from current HEAD of the repo
    parent_commit = ssh_check(
        host,
        f"git -C {quoted_repo} rev-parse HEAD",
        error_context="get parent commit",
        os_type=os_type,
    ).strip()

    quoted_wt = qrp(worktree_path, os_type=os_type)
    quoted_branch = f'"{branch}"' if os_type == "windows" else shlex.quote(branch)
    quoted_parent = (
        f'"{parent_commit}"' if os_type == "windows" else shlex.quote(parent_commit)
    )

    ssh_check(
        host,
        f"git -C {quoted_repo} worktree add -b {quoted_branch} "
        f"{quoted_wt} {quoted_parent}",
        error_context="create worktree",
        os_type=os_type,
    )

    return worktree_path, parent_commit


def seed_settings(
    host: str,
    worktree_path: str,
    allowlist: list[str],
    *,
    os_type: str | None = None,
) -> None:
    """
    Write a narrow .claude/settings.local.json into the worktree.
    This gives the spawned `claude -p` session exactly the permissions
    it needs, without requiring --permission-mode bypassPermissions.
    """
    settings = {
        "permissions": {
            "allow": allowlist,
        }
    }
    sep = "\\" if (os_type == "windows" and "/" not in worktree_path) else "/"
    settings_path = f"{worktree_path}{sep}.claude{sep}settings.local.json"
    ssh_put_file(
        host,
        settings_path,
        json.dumps(settings, indent=2) + "\n",
        os_type=os_type,
    )


def _build_claude_args(permission_mode: str, model: str | None) -> list[str]:
    return ["claude", "-p", "--permission-mode", permission_mode]


def _build_agy_args(permission_mode: str, model: str | None) -> list[str]:
    args = ["agy", "--print", "PROMPT_PLACEHOLDER"]
    if permission_mode == "plan":
        args.extend(["--mode", "plan"])
    elif permission_mode in ("acceptEdits", "bypassPermissions"):
        args.extend(["--dangerously-skip-permissions", "--mode", "accept-edits"])
    if model:
        args.extend(["--model", model])
    return args


def _build_opencode_args(permission_mode: str, model: str | None) -> list[str]:
    args = ["opencode", "run"]
    if permission_mode in ("acceptEdits", "bypassPermissions"):
        args.append("--auto")
    if model:
        args.extend(["--model", model])
    args.append("PROMPT_PLACEHOLDER")
    return args


def _build_pi_args(permission_mode: str, model: str | None) -> list[str]:
    args = ["pi", "-p"]
    if model:
        args.extend(["--model", model])
    args.append("PROMPT_PLACEHOLDER")
    return args


def _build_codex_args(permission_mode: str, model: str | None) -> list[str]:
    args = ["codex", "exec"]
    if permission_mode in ("acceptEdits", "bypassPermissions"):
        args.append("--dangerously-bypass-approvals-and-sandbox")
    if model:
        args.extend(["--model", model])
    args.append("PROMPT_PLACEHOLDER")
    return args


_AGENT_ARG_BUILDERS = {
    "claude": _build_claude_args,
    "agy": _build_agy_args,
    "opencode": _build_opencode_args,
    "pi": _build_pi_args,
    "codex": _build_codex_args,
}


def build_agent_args(
    agent: str,
    permission_mode: str,
    model: str | None = None,
) -> list[str]:
    builder = _AGENT_ARG_BUILDERS.get(agent)
    if builder is None:
        raise ValueError(f"Unknown agent: {agent}")
    return builder(permission_mode, model)


def run_remote_agent(
    host: str,
    worktree_path: str,
    prompt: str,
    permission_mode: str,
    timeout: float,
    agent: str,
    model: str | None = None,
    *,
    os_type: str | None = None,
) -> tuple[int, str, str]:
    """
    Invoke the selected agent CLI on the remote in the worktree directory.
    Returns (exit_code, stdout, stderr).
    """
    if os_type is None:
        os_type = detect_remote_os(host)

    sep = "\\" if (os_type == "windows" and "/" not in worktree_path) else "/"
    prompt_file = f"{worktree_path}{sep}.agent-prompt.txt"
    ssh_put_file(host, prompt_file, prompt, os_type=os_type)

    quoted_wt = qrp(worktree_path, os_type=os_type)
    quoted_prompt = qrp(prompt_file, os_type=os_type)

    args_list = build_agent_args(agent, permission_mode, model)

    if os_type == "windows":
        args_json = json.dumps(args_list)
        if agent == "claude":
            py_cmd = (
                "import json, os, pathlib, shutil, subprocess, sys\n"
                f"args = json.loads({args_json!r})\n"
                "prog = shutil.which(args[0]) or args[0]\n"
                "is_batch = str(prog).lower().endswith(('.cmd', '.bat'))\n"
                "if is_batch:\n"
                "    prog_quoted = '\"' + str(prog) + '\"' if not (str(prog).startswith('\"') and str(prog).endswith('\"')) else str(prog)\n"
                "    batch_arguments = subprocess.list2cmdline(args[1:])\n"
                "    batch_command = (prog_quoted + ' ' + batch_arguments).strip()\n"
                "    command = 'cmd.exe /d /c \"' + batch_command + '\"'\n"
                f"    with open({prompt_file!r}, 'r', encoding='utf-8') as f:\n"
                f"        res = subprocess.run(command, stdin=f, cwd={worktree_path!r}, shell=False)\n"
                "else:\n"
                "    args[0] = prog\n"
                f"    with open({prompt_file!r}, 'r', encoding='utf-8') as f:\n"
                f"        res = subprocess.run(args, stdin=f, cwd={worktree_path!r}, shell=False)\n"
                "sys.exit(res.returncode)\n"
            )
        else:
            py_cmd = (
                "import json, os, pathlib, shutil, subprocess, sys\n"
                f"args = json.loads({args_json!r})\n"
                "prog = shutil.which(args[0]) or args[0]\n"
                "is_batch = str(prog).lower().endswith(('.cmd', '.bat'))\n"
                "if is_batch:\n"
                "    if 'PROMPT_PLACEHOLDER' in args:\n"
                "        args.remove('PROMPT_PLACEHOLDER')\n"
                "    prog_quoted = '\"' + str(prog) + '\"' if not (str(prog).startswith('\"') and str(prog).endswith('\"')) else str(prog)\n"
                "    batch_arguments = subprocess.list2cmdline(args[1:])\n"
                "    batch_command = (prog_quoted + ' ' + batch_arguments).strip()\n"
                "    command = 'cmd.exe /d /c \"' + batch_command + '\"'\n"
                f"    with open({prompt_file!r}, 'r', encoding='utf-8') as f:\n"
                f"        res = subprocess.run(command, stdin=f, cwd={worktree_path!r}, shell=False)\n"
                "else:\n"
                "    args[0] = prog\n"
                "    if 'PROMPT_PLACEHOLDER' in args:\n"
                f"        args[args.index('PROMPT_PLACEHOLDER')] = pathlib.Path({prompt_file!r}).read_text(encoding='utf-8')\n"
                f"    res = subprocess.run(args, cwd={worktree_path!r}, shell=False)\n"
                "sys.exit(res.returncode)\n"
            )
        b64_code = base64.b64encode(py_cmd.encode("utf-8")).decode("ascii")
        py_bootstrap = (
            f"import base64; exec(base64.b64decode('{b64_code}').decode('utf-8'))"
        )
        remote_cmd = f'python -c "{py_bootstrap}"'
    else:
        if agent == "claude":
            remote_cmd = (
                f"cd {quoted_wt} && "
                f"claude -p --permission-mode {shlex.quote(permission_mode)} "
                f"< {quoted_prompt}"
            )
        else:
            args_json = json.dumps(args_list)
            py_cmd = (
                "import json, pathlib, subprocess, sys; "
                f"args = json.loads({args_json!r}); "
                f"args[args.index('PROMPT_PLACEHOLDER')] = pathlib.Path({prompt_file!r}).read_text(encoding='utf-8'); "
                "sys.exit(subprocess.run(args).returncode)"
            )
            remote_cmd = f"cd {quoted_wt} && python3 -c {shlex.quote(py_cmd)}"

    result = ssh_run(host, remote_cmd, timeout=timeout, os_type=os_type)
    stderr = result.stderr

    # Cleanup remains best-effort because the agent result is the primary outcome.
    try:
        if os_type == "windows":
            rm_cmd = (
                'python -c "import os, sys; '
                'os.path.exists(sys.argv[1]) and os.remove(sys.argv[1])" '
                f"{quoted_prompt}"
            )
        else:
            rm_cmd = f"rm -f {quoted_prompt}"
        ssh_run(host, rm_cmd, os_type=os_type)
    except (OSError, subprocess.SubprocessError) as exception:
        cleanup_warning = f"warning: could not remove remote prompt file: {exception}"
        stderr = "\n".join(part for part in (stderr, cleanup_warning) if part)

    return result.returncode, result.stdout, stderr


def collect_result(
    host: str,
    worktree_path: str,
    branch: str,
    parent_commit: str,
    agent_exit_code: int,
    agent_stdout: str,
    agent_stderr: str,
    *,
    os_type: str | None = None,
) -> RunResult:
    """
    After the agent exits, figure out what changed in the worktree and
    build a RunResult.
    """
    if os_type is None:
        os_type = detect_remote_os(host)

    quoted_wt = qrp(worktree_path, os_type=os_type)
    quoted_parent = (
        f'"{parent_commit}"' if os_type == "windows" else shlex.quote(parent_commit)
    )

    new_commit_raw = ssh_check(
        host,
        f"git -C {quoted_wt} rev-parse HEAD",
        os_type=os_type,
    ).strip()
    new_commit: str | None = new_commit_raw if new_commit_raw != parent_commit else None

    # Files changed: diff against parent commit (includes committed changes)
    # plus any uncommitted changes (staged + unstaged + untracked).
    if os_type == "windows":
        diff_out1 = ssh_run(
            host,
            f"git -C {quoted_wt} diff --name-only {quoted_parent}",
            os_type=os_type,
        ).stdout
        diff_out2 = ssh_run(
            host,
            f"git -C {quoted_wt} ls-files --others --exclude-standard",
            os_type=os_type,
        ).stdout
        diff_out = f"{diff_out1}\n{diff_out2}"
    else:
        diff_cmd = (
            f"git -C {quoted_wt} diff --name-only {quoted_parent} && "
            f"git -C {quoted_wt} ls-files --others --exclude-standard"
        )
        diff_out = ssh_run(host, diff_cmd, os_type=os_type).stdout
    files_changed = sorted(
        {line.strip() for line in diff_out.splitlines() if line.strip()}
    )

    cleanup_command = (
        f"python agent-remote.py cleanup "
        f"--host {shlex.quote(host)} "
        f"--branch {shlex.quote(branch)}"
    )

    return RunResult(
        host=host,
        branch=branch,
        worktree_path=worktree_path,
        parent_commit=parent_commit,
        new_commit=new_commit,
        files_changed=files_changed,
        agent_exit_code=agent_exit_code,
        # Tail size: enough to capture verification output from a multi-step
        # remote agent (systemctl status, journalctl excerpts, benchmark
        # matrices, etc.).
        stdout_tail=agent_stdout[-20000:] if agent_stdout else "",
        stderr_tail=agent_stderr[-5000:] if agent_stderr else "",
        cleanup_command=cleanup_command,
    )


def cmd_run(args: argparse.Namespace) -> int:
    # Reverse Git Bash MSYS path mangling on user-supplied remote paths
    args.repo_path = unmangle_msys_path(args.repo_path)

    # Permission mode guardrail
    if (
        args.permission_mode == "bypassPermissions"
        and os.environ.get("REMOTE_AGENT_ALLOW_BYPASS") != "1"
    ):
        print(
            "refused: --permission-mode bypassPermissions requires "
            "REMOTE_AGENT_ALLOW_BYPASS=1 in the environment.",
            file=sys.stderr,
        )
        return 2

    # Auto-generate branch name if not provided
    branch = args.branch or f"agent-remote/auto-{int(time.time())}"

    timeout_str = os.environ.get("REMOTE_AGENT_TIMEOUT") or "3600"
    timeout = float(timeout_str)

    try:
        os_type = getattr(args, "os", "auto")
        if os_type == "auto":
            os_type = detect_remote_os(args.host)
        else:
            set_host_os(args.host, os_type)

        worktree_path, parent_commit = ensure_worktree(
            args.host,
            args.repo_path,
            branch,
            os_type=os_type,
        )

        # Resolve agent
        agent = args.agent
        if not agent:
            if os.environ.get("ANTIGRAVITY_AGENT") == "1":
                agent = "agy"
            elif any(k.startswith("OPENCODE_") for k in os.environ):
                agent = "opencode"
            elif (
                any(k.startswith("CLAUDE_CODE") for k in os.environ)
                or os.environ.get("CLAUDE_CODE_SUBPROCESS_ENV_SCRUB") == "1"
            ):
                agent = "claude"
            elif any(k.startswith("PI_") for k in os.environ):
                agent = "pi"
            elif any(k.startswith("CODEX_") for k in os.environ):
                agent = "codex"
            else:
                agent = "opencode"

        # Seed settings only for Claude
        if agent == "claude":
            allowlist = list(DEFAULT_REMOTE_ALLOWLIST)
            if args.extra_allow:
                allowlist.extend(args.extra_allow)
            seed_settings(args.host, worktree_path, allowlist, os_type=os_type)

        exit_code, stdout, stderr = run_remote_agent(
            host=args.host,
            worktree_path=worktree_path,
            prompt=args.prompt,
            permission_mode=args.permission_mode,
            timeout=timeout,
            agent=agent,
            model=args.model,
            os_type=os_type,
        )

        result = collect_result(
            args.host,
            worktree_path,
            branch,
            parent_commit,
            exit_code,
            stdout,
            stderr,
            os_type=os_type,
        )
        print(result.to_json())
        return 0 if result.success else 1

    except subprocess.TimeoutExpired:
        print(
            json.dumps(
                {
                    "success": False,
                    "error": "timeout",
                    "host": args.host,
                    "branch": branch,
                    "timeout_seconds": timeout,
                },
                indent=2,
            ),
            file=sys.stdout,
        )
        return 3
    except Exception as e:  # noqa: BLE001 - boundary catch-all, failure is reported not raised
        print(
            json.dumps(
                {
                    "success": False,
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "host": args.host,
                    "branch": branch,
                },
                indent=2,
            ),
            file=sys.stdout,
        )
        return 4


def cmd_cleanup(args: argparse.Namespace) -> int:
    """
    Remove a worktree and its branch from the remote.
    Does NOT delete any commits - if the caller merged the branch elsewhere,
    those commits survive.
    """
    if not args.repo_path:
        print(
            "cleanup: --repo-path is required so we can find the worktree",
            file=sys.stderr,
        )
        return 2
    args.repo_path = unmangle_msys_path(args.repo_path)

    try:
        os_type = getattr(args, "os", "auto")
        if os_type == "auto":
            os_type = detect_remote_os(args.host)
        else:
            set_host_os(args.host, os_type)

        worktree_path = compute_worktree_path(
            args.repo_path, args.branch, os_type=os_type
        )
        quoted_repo = qrp(args.repo_path, os_type=os_type)
        quoted_wt = qrp(worktree_path, os_type=os_type)
        quoted_branch = (
            f'"{args.branch}"' if os_type == "windows" else shlex.quote(args.branch)
        )
        if os_type == "windows":
            ssh_check(
                args.host,
                f"git -C {quoted_repo} worktree remove --force {quoted_wt}",
                error_context="remove worktree",
                os_type=os_type,
            )
            ssh_check(
                args.host,
                f"git -C {quoted_repo} branch -D {quoted_branch}",
                error_context="remove branch",
                os_type=os_type,
            )
        else:
            ssh_check(
                args.host,
                f"git -C {quoted_repo} worktree remove --force {quoted_wt} && "
                f"git -C {quoted_repo} branch -D {quoted_branch}",
                error_context="remove worktree and branch",
                os_type=os_type,
            )
        print(
            json.dumps(
                {
                    "success": True,
                    "host": args.host,
                    "branch": args.branch,
                    "removed": worktree_path,
                },
                indent=2,
            )
        )
        return 0
    except Exception as e:  # noqa: BLE001 - boundary catch-all, failure is reported not raised
        print(
            json.dumps(
                {
                    "success": False,
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
                indent=2,
            ),
            file=sys.stdout,
        )
        return 1


def build_probe_command(repo_path: str, os_type: str) -> str:
    """
    Build the OS-appropriate probe.

    POSIX remotes get a shell command line; Windows remotes get PowerShell
    script text for `ssh_run_powershell`.
    """
    if os_type == "windows":
        ps_script = (
            "$ErrorActionPreference = 'SilentlyContinue'; "
            f"$repoPath = {ps_literal(repo_path)}; "
            "$gitVer = (git --version 2>$null | Out-String).Trim(); "
            "$claudeVer = (claude --version 2>$null | Out-String).Trim(); "
            "$agyVer = (agy --version 2>$null | Out-String).Trim(); "
            "$opencodeVer = (opencode --version 2>$null | Out-String).Trim(); "
            "$piVer = (pi --version 2>$null | Out-String).Trim(); "
            "$codexVer = (codex --version 2>$null | Out-String).Trim(); "
            "$pyVer = (python --version 2>$null | Out-String).Trim(); "
            "if (-not $pyVer) { $pyVer = (python3 --version 2>$null | Out-String).Trim() }; "
            "if (-not $pyVer) { $pyVer = (py --version 2>$null | Out-String).Trim() }; "
            "$repoExists = $false; "
            "if ($repoPath) { "
            "$gitDir = Join-Path $repoPath '.git'; "
            "$repoExists = Test-Path $gitDir "
            "}; "
            "$verOut = (cmd /c ver | Out-String).Trim(); "
            "$procPath = (Get-Process -Id $PID).Path; "
            "if (-not $procPath) { $procPath = 'powershell.exe' }; "
            "$confShell = if ($env:COMSPEC) { $env:COMSPEC } else { 'cmd.exe' }; "
            "$data = [ordered]@{ "
            "os = 'windows'; "
            "shell = $procPath; "
            "configured_shell = $confShell; "
            "user = if ($env:USERNAME) { $env:USERNAME } else { (whoami 2>$null | Out-String).Trim() }; "
            "hostname = if ($env:COMPUTERNAME) { $env:COMPUTERNAME } else { (hostname 2>$null | Out-String).Trim() }; "
            "uname = $verOut; "
            "repo_path_exists = $repoExists; "
            "git = if ($gitVer) { $gitVer } else { 'missing' }; "
            "claude = if ($claudeVer) { $claudeVer } else { 'missing' }; "
            "agy = if ($agyVer) { $agyVer } else { 'missing' }; "
            "opencode = if ($opencodeVer) { $opencodeVer } else { 'missing' }; "
            "pi = if ($piVer) { $piVer } else { 'missing' }; "
            "codex = if ($codexVer) { $codexVer } else { 'missing' }; "
            "python = if ($pyVer) { $pyVer } else { 'missing' }; "
            "path = if ($env:PATH) { $env:PATH } else { '' } "
            "}; "
            "$data | ConvertTo-Json -Compress"
        )
        return ps_script

    return (
        "printf '{'; "
        'printf \'"os":"%s",\' "posix"; '
        'printf \'"shell":"%s",\' "${BASH:-$0}"; '
        'printf \'"configured_shell":"%s",\' "${SHELL:-}"; '
        'printf \'"user":"%s",\' "$(whoami)"; '
        'printf \'"hostname":"%s",\' "$(hostnamectl --static 2>/dev/null || cat /etc/hostname 2>/dev/null || hostname 2>/dev/null)"; '
        'printf \'"uname":"%s",\' "$(uname -srm)"; '
        f"printf '\"repo_path_exists\":%s,' "
        f'"$(test -d {qrp(repo_path)}/.git && echo true || echo false)"; '
        'printf \'"git":"%s",\' "$(git --version 2>/dev/null || echo missing)"; '
        'printf \'"claude":"%s",\' "$(claude --version 2>/dev/null || echo missing)"; '
        'printf \'"agy":"%s",\' "$(agy --version 2>/dev/null || echo missing)"; '
        'printf \'"opencode":"%s",\' "$(opencode --version 2>/dev/null || echo missing)"; '
        'printf \'"pi":"%s",\' "$(pi --version 2>/dev/null || echo missing)"; '
        'printf \'"codex":"%s",\' "$(codex --version 2>/dev/null || echo missing)"; '
        'printf \'"python":"%s",\' "$(python3 --version 2>/dev/null || echo missing)"; '
        'printf \'"path":"%s"\' "$PATH"; '
        "printf '}\\n'"
    )


def cmd_probe(args: argparse.Namespace) -> int:
    """
    Sanity-check the remote environment. Prints a JSON object with:
    os, shell, configured_shell, user, hostname, uname, repo_path_exists,
    git, claude, agy, opencode, pi, codex, python, path, plus success and host.
    """
    args.repo_path = unmangle_msys_path(args.repo_path)

    try:
        os_type = getattr(args, "os", "auto")
        if os_type == "auto":
            os_type = detect_remote_os(args.host)
        else:
            set_host_os(args.host, os_type)

        probe = build_probe_command(args.repo_path, os_type)
        if os_type == "windows":
            result = ssh_run_powershell(args.host, probe)
        else:
            result = ssh_run(args.host, probe, os_type=os_type)
        if result.returncode != 0:
            print(
                json.dumps(
                    {
                        "success": False,
                        "error": "probe command failed",
                        "stderr": result.stderr,
                    },
                    indent=2,
                )
            )
            return 1
        # Validate JSON, re-emit
        parsed = json.loads(result.stdout)
        parsed["success"] = True
        parsed["host"] = args.host
        print(json.dumps(parsed, indent=2))
        return 0
    except Exception as e:  # noqa: BLE001 - boundary catch-all, failure is reported not raised
        print(
            json.dumps(
                {
                    "success": False,
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
                indent=2,
            )
        )
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-remote",
        description="Spawn an agent session on a remote host in an isolated worktree.",
    )
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    # run
    p_run = subparsers.add_parser(
        "run",
        help="Run a prompt on a remote host in a fresh worktree.",
    )
    p_run.add_argument("--host", required=True, help="user@hostname for ssh")
    p_run.add_argument(
        "--repo-path", required=True, help="path to the existing git repo on the remote"
    )
    p_run.add_argument(
        "--prompt", required=True, help="prompt to pass to the remote agent session"
    )
    p_run.add_argument(
        "--branch",
        default=None,
        help="branch name for the worktree (default: agent-remote/auto-<epoch>)",
    )
    p_run.add_argument(
        "--permission-mode",
        default="acceptEdits",
        choices=["default", "acceptEdits", "bypassPermissions", "plan"],
        help="permission mode for the spawned agent",
    )
    p_run.add_argument(
        "--extra-allow",
        action="append",
        default=[],
        help="additional permission rule(s) to add to the remote settings.local.json (only for claude)",
    )
    p_run.add_argument(
        "--agent",
        default=None,
        choices=["claude", "opencode", "agy", "pi", "codex"],
        help="agent runner to spawn on the remote (defaults to auto-detection: agy if ANTIGRAVITY_AGENT=1, else opencode)",
    )
    p_run.add_argument(
        "--model",
        "-m",
        default=None,
        help="model/provider to use for the agent session (e.g. ollama/<model-id>, only for agy/opencode/pi/codex)",
    )
    p_run.add_argument(
        "--os",
        default="auto",
        choices=["auto", "windows", "posix"],
        help="remote host operating system (default: auto-detected)",
    )
    p_run.set_defaults(func=cmd_run)

    # cleanup
    p_cleanup = subparsers.add_parser(
        "cleanup",
        help="Remove a worktree and its branch on the remote.",
    )
    p_cleanup.add_argument("--host", required=True)
    p_cleanup.add_argument("--branch", required=True)
    p_cleanup.add_argument("--repo-path", required=True)
    p_cleanup.add_argument(
        "--os",
        default="auto",
        choices=["auto", "windows", "posix"],
        help="remote host operating system (default: auto-detected)",
    )
    p_cleanup.set_defaults(func=cmd_cleanup)

    # probe
    p_probe = subparsers.add_parser(
        "probe",
        help="Sanity-check a remote host's environment.",
    )
    p_probe.add_argument("--host", required=True)
    p_probe.add_argument("--repo-path", required=True)
    p_probe.add_argument(
        "--os",
        default="auto",
        choices=["auto", "windows", "posix"],
        help="remote host operating system (default: auto-detected)",
    )
    p_probe.set_defaults(func=cmd_probe)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
