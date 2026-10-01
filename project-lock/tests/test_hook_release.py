from __future__ import annotations

import importlib.util
import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from project_lock import core
from project_lock.command_authorization import authorizes_release


@pytest.fixture
def release_case(tmp_path, monkeypatch):
    root = tmp_path / "locked project"
    root.mkdir()
    monkeypatch.setattr(core, "run_git", lambda *arguments: None)
    metadata = core.acquire(
        root, reason="synthetic owner", duration=timedelta(minutes=5), session="old"
    )
    hook_path = Path(__file__).parents[1] / "hooks" / "pre_tool_use.py"
    specification = importlib.util.spec_from_file_location("release_hook", hook_path)
    hook = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(hook)
    script = hook_path.parent.parent / "scripts" / "project-lock.py"
    command = f'"{sys.executable}" "{script}" release "{root}" --lock-id {metadata["lock_id"]}'
    payload = {"tool_name": "Bash", "cwd": str(root), "session_id": "new"}
    return root, metadata, hook, command, payload


@pytest.mark.parametrize("outside", [False, True])
@pytest.mark.parametrize("interpreter", ["absolute", "python"])
def test_owner_token_allows_release_from_another_session(release_case, outside, interpreter):
    root, metadata, hook, command, payload = release_case
    if interpreter == "python":
        command = command.replace(f'"{sys.executable}"', "python")
    if outside:
        payload["cwd"] = str(root.parent)
    script = Path(hook.__file__).parent.parent / "scripts" / "project-lock.py"
    assert authorizes_release(command, payload["cwd"], script)
    assert hook.evaluate({**payload, "tool_input": {"command": command}}) == (0, "")
    assert core.inspect(root)["lock"]["lock_id"] == metadata["lock_id"]
    assert core.release(root, lock_id=metadata["lock_id"])


@pytest.mark.parametrize(
    "alteration",
    [
        "wrong-token",
        "other-script",
        "other-interpreter",
        "chain",
        "substitution",
        "extra-option",
        "unclosed-quote",
        "missing-metadata",
        "other-operation",
        "other-option",
        "missing-argument",
    ],
)
def test_release_exception_does_not_authorize_unproven_commands(release_case, alteration):
    root, metadata, hook, command, payload = release_case
    if alteration == "wrong-token":
        command = command.replace(metadata["lock_id"], "wrong")
    elif alteration == "other-script":
        command = command.replace("project-lock.py", "other.py")
    elif alteration == "other-interpreter":
        command = command.replace(sys.executable, "other-interpreter")
    elif alteration == "chain":
        command += "; echo extra"
    elif alteration == "substitution":
        command = command.replace(metadata["lock_id"], "$(echo token)")
    elif alteration == "extra-option":
        command += " --force"
    elif alteration == "unclosed-quote":
        command += ' "unterminated'
    elif alteration == "missing-metadata":
        core.metadata_path(core.resolve_root(root)).write_text(json.dumps({}), encoding="utf-8")
    elif alteration == "other-operation":
        command = command.replace(" release ", " renew ")
    elif alteration == "other-option":
        command = command.replace("--lock-id", "--expect-lock-id")
    elif alteration == "missing-argument":
        command = command.replace(metadata["lock_id"], "")
    assert hook.evaluate({**payload, "tool_input": {"command": command}})[0] == 2


def test_release_exception_requires_existing_lock(release_case):
    root, metadata, hook, command, payload = release_case
    core.release(root, lock_id=metadata["lock_id"])
    script = Path(hook.__file__).parent.parent / "scripts" / "project-lock.py"
    assert not authorizes_release(command, payload["cwd"], script)


@pytest.mark.parametrize("thread_identity", ["child-thread", ""])
def test_codex_session_identity_matches_hook_for_subagent(
    release_case, monkeypatch, thread_identity
):
    root, metadata, hook, _command, _payload = release_case
    core.release(root, lock_id=metadata["lock_id"])
    monkeypatch.setenv("CODEX_SESSION_ID", "hook-session")
    monkeypatch.setenv("CODEX_THREAD_ID", thread_identity)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "inherited-claude")
    monkeypatch.setenv("CLAUDE_PID", "4242")
    acquired = core.acquire(root, reason="subagent edit", duration=timedelta(minutes=1))
    assert acquired["session"] == "hook-session"
    assert acquired["owner_pid"] is None
    assert hook.evaluate(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": str(root / "example.txt")},
            "session_id": "hook-session",
        }
    ) == (0, "")
