from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

from project_lock import core

HOOK_PATH = Path(__file__).parents[1] / "hooks" / "pre_tool_use.py"


def load_hook_module():
    """Load hooks/pre_tool_use.py in-process for direct function-level assertions."""
    spec = importlib.util.spec_from_file_location("project_lock_hook_under_test", HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_hook(payload: dict, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, **(extra_env or {})}
    return subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def edit_payload(target: Path, session_id: str = "session-a") -> dict:
    return {
        "session_id": session_id,
        "cwd": str(target.parent),
        "tool_name": "Edit",
        "tool_input": {"file_path": str(target)},
    }


def test_foreign_lock_denies_edit(nested_worktree_repo):
    main = nested_worktree_repo["main"]
    core.acquire(main, reason="busy", duration=timedelta(minutes=5), session="session-b")
    result = run_hook(edit_payload(main / "README.md", session_id="session-a"))
    assert result.returncode == 2
    assert "busy" in result.stderr
    assert "session-b" in result.stderr


def test_foreign_lock_allows_plain_powershell_reads_but_not_expressions(nested_worktree_repo):
    root = nested_worktree_repo["main"]
    core.acquire(root, reason="busy", duration=timedelta(minutes=5), session="other")
    hook = load_hook_module()
    payload = {"session_id": "reader", "cwd": str(root), "tool_name": "Bash"}
    for command in (
        "Get-Content -LiteralPath README.md",
        "get-item README.md",
        "Get-ChildItem -Name",
        "Select-String -Pattern test README.md",
        "Test-Path README.md",
        "Get-FileHash README.md",
        "Get-Command git",
        "Write-Output hello",
    ):
        assert hook.evaluate({**payload, "tool_input": {"command": command}})[0] == 0
    for command in (
        "Get-Content (Remove-Item README.md)",
        "Get-Content { Remove-Item README.md }",
        "Get-Content README.md; Remove-Item README.md",
        "Get-Content README.md > copied.txt",
        "Get-Content $(Remove-Item README.md)",
    ):
        assert hook.evaluate({**payload, "tool_input": {"command": command}})[0] == 2


def test_relative_target_resolves_against_payload_cwd(nested_worktree_repo):
    main = nested_worktree_repo["main"]
    core.acquire(main, reason="busy", duration=timedelta(minutes=5), session="session-b")
    payload = {
        "session_id": "session-a",
        "cwd": str(main),
        "tool_name": "Edit",
        "tool_input": {"file_path": "README.md"},
    }
    result = run_hook(payload)
    assert result.returncode == 2
    assert "busy" in result.stderr


def test_own_session_lock_allows_edit(nested_worktree_repo):
    main = nested_worktree_repo["main"]
    core.acquire(main, reason="mine", duration=timedelta(minutes=5), session="session-a")
    result = run_hook(edit_payload(main / "README.md", session_id="session-a"))
    assert result.returncode == 0


def test_sessionless_legacy_lock_denies(nested_worktree_repo):
    main = nested_worktree_repo["main"]
    core.acquire(main, reason="manual", duration=timedelta(minutes=5))
    result = run_hook(edit_payload(main / "README.md"))
    assert result.returncode == 2
    assert "session unknown" in result.stderr


def test_no_lock_denies_with_acquire_recipe(nested_worktree_repo):
    main = nested_worktree_repo["main"]
    result = run_hook(edit_payload(main / "README.md", session_id="session-a"))
    assert result.returncode == 2
    assert "acquire" in result.stderr
    assert "--session session-a" in result.stderr


def test_scratch_path_with_no_git_ancestor_under_temp_allows_edit(tmp_path):
    # tmp_path is itself under the system temp dir, so a non-Git target below
    # it is true scratch space: no project root to coordinate.
    target = tmp_path / "scratch" / "notes.txt"
    result = run_hook(edit_payload(target))
    assert result.returncode == 0
    assert result.stderr == ""


def test_scratch_path_with_no_git_ancestor_under_temp_allows_edit_in_process(tmp_path):
    hook_module = load_hook_module()
    target = tmp_path / "scratch" / "notes.txt"
    decision, message = hook_module.check_file_tool(edit_payload(target))
    assert decision == hook_module.ALLOW
    assert message == ""


def test_non_git_path_outside_temp_denies_with_acquire_recipe(tmp_path, monkeypatch):
    # Simulate a non-Git project directory that is NOT scratch (e.g.
    # ~/Documents/notes) by making is_under_temp_dir() see no recognized temp
    # roots, even though tmp_path physically lives under one. The owner's
    # review point: agents must still coordinate writes here, so this must be
    # denied with an acquire recipe, not silently allowed.
    monkeypatch.setattr(core, "_temp_roots", list)
    hook_module = load_hook_module()
    target = tmp_path / "notes" / "file.txt"
    decision, message = hook_module.check_file_tool(edit_payload(target))
    assert decision == hook_module.DENY
    assert "acquire" in message


def test_nested_worktree_not_governed_by_parent_lock(nested_worktree_repo):
    main = nested_worktree_repo["main"]
    nested = nested_worktree_repo["nested"]
    core.acquire(main, reason="busy", duration=timedelta(minutes=5), session="session-b")
    core.acquire(nested, reason="mine", duration=timedelta(minutes=5), session="session-a")
    result = run_hook(edit_payload(nested / "inner.py", session_id="session-a"))
    assert result.returncode == 0


def test_warn_mode_allows_with_message(nested_worktree_repo):
    main = nested_worktree_repo["main"]
    core.acquire(main, reason="busy", duration=timedelta(minutes=5), session="session-b")
    result = run_hook(edit_payload(main / "README.md"), {"PROJECT_LOCK_ENFORCE": "warn"})
    assert result.returncode == 0
    assert "busy" in result.stderr


def test_off_mode_allows_silently(nested_worktree_repo):
    main = nested_worktree_repo["main"]
    core.acquire(main, reason="busy", duration=timedelta(minutes=5), session="session-b")
    result = run_hook(edit_payload(main / "README.md"), {"PROJECT_LOCK_ENFORCE": "off"})
    assert result.returncode == 0
    assert result.stderr == ""


def test_malformed_input_fails_open():
    result = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input="not json",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0


def test_unknown_tool_allows():
    result = run_hook({"session_id": "s", "cwd": ".", "tool_name": "Glob", "tool_input": {}})
    assert result.returncode == 0


def test_governing_lock_in_process_latency(nested_worktree_repo):
    main = nested_worktree_repo["main"]
    core.acquire(main, reason="busy", duration=timedelta(minutes=5))
    target = main / "src" / "deep" / "file.py"
    start = time.perf_counter()
    for _ in range(20):
        core.governing_lock(target)
    elapsed = (time.perf_counter() - start) / 20
    assert elapsed < 0.05


def test_hook_process_end_to_end_budget(nested_worktree_repo):
    main = nested_worktree_repo["main"]
    start = time.perf_counter()
    run_hook(edit_payload(main / "README.md"))
    assert time.perf_counter() - start < 3.0


def bash_payload(command: str, cwd: Path, session_id: str = "session-a") -> dict:
    return {
        "session_id": session_id,
        "cwd": str(cwd),
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }


def test_bash_in_foreign_cwd_denies_mutation(nested_worktree_repo):
    main = nested_worktree_repo["main"]
    core.acquire(main, reason="busy", duration=timedelta(minutes=5), session="session-b")
    result = run_hook(bash_payload("touch new.txt", main))
    assert result.returncode == 2


def test_bash_in_foreign_cwd_allows_read_only(nested_worktree_repo):
    main = nested_worktree_repo["main"]
    core.acquire(main, reason="busy", duration=timedelta(minutes=5), session="session-b")
    for command in ("ls -la", "git status", "git log --oneline -5", "rg pattern ."):
        result = run_hook(bash_payload(command, main))
        assert result.returncode == 0, command


def test_bash_compound_with_mutating_segment_denies(nested_worktree_repo):
    main = nested_worktree_repo["main"]
    core.acquire(main, reason="busy", duration=timedelta(minutes=5), session="session-b")
    result = run_hook(bash_payload("git status && git commit -m x", main))
    assert result.returncode == 2


def test_bash_own_session_cwd_allows(nested_worktree_repo):
    main = nested_worktree_repo["main"]
    core.acquire(main, reason="mine", duration=timedelta(minutes=5), session="session-a")
    result = run_hook(bash_payload("touch new.txt", main))
    assert result.returncode == 0


def test_bash_unlocked_cwd_allows(nested_worktree_repo):
    result = run_hook(bash_payload("touch new.txt", nested_worktree_repo["sibling"]))
    assert result.returncode == 0


def test_bash_absolute_path_token_under_foreign_lock_denies(nested_worktree_repo, tmp_path):
    main = nested_worktree_repo["main"]
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    core.acquire(main, reason="busy", duration=timedelta(minutes=5), session="session-b")
    result = run_hook(bash_payload(f'echo x > "{main / "out.txt"}"', outside))
    assert result.returncode == 2
    assert str(core.canonical_path(main)) in result.stderr


def test_bash_path_token_scan_excludes_own_session_lock(nested_worktree_repo):
    main = nested_worktree_repo["main"]
    sibling = nested_worktree_repo["sibling"]
    core.acquire(main, reason="mine", duration=timedelta(minutes=5), session="session-a")
    result = run_hook(
        bash_payload(f'echo x > "{main / "out.txt"}"', sibling, session_id="session-a")
    )
    assert result.returncode == 0


def test_bash_registry_lock_roots_skips_corrupt_and_orphaned_entries(
    nested_worktree_repo, tmp_path
):
    sibling = nested_worktree_repo["sibling"]

    corrupt_entry = core.state_directory() / "locks" / "corrupt.json"
    corrupt_entry.parent.mkdir(parents=True, exist_ok=True)
    corrupt_entry.write_text("not valid json", encoding="utf-8")

    orphan = tmp_path / "orphan"
    orphan.mkdir()
    core.acquire(orphan, reason="busy", duration=timedelta(minutes=5), session="session-b")
    shutil.rmtree(orphan / core.MARKER_DIRECTORY_NAME)

    hook_module = load_hook_module()
    assert hook_module.registry_lock_roots() == []

    result = run_hook(bash_payload(f'echo x > "{orphan / "out.txt"}"', sibling))
    assert result.returncode == 0
    assert result.stderr == ""


def test_bash_redirection_segment_denies(nested_worktree_repo):
    main = nested_worktree_repo["main"]
    core.acquire(main, reason="busy", duration=timedelta(minutes=5), session="session-b")
    result = run_hook(bash_payload("echo x > out.txt", main))
    assert result.returncode == 2


def test_bash_newline_separated_mutating_segment_denies(nested_worktree_repo):
    main = nested_worktree_repo["main"]
    core.acquire(main, reason="busy", duration=timedelta(minutes=5), session="session-b")
    result = run_hook(bash_payload("git status\ntouch x", main))
    assert result.returncode == 2


def test_bash_command_substitution_denies(nested_worktree_repo):
    main = nested_worktree_repo["main"]
    core.acquire(main, reason="busy", duration=timedelta(minutes=5), session="session-b")
    result = run_hook(bash_payload("cat $(mutate)", main))
    assert result.returncode == 2


def test_bash_process_substitution_denies(nested_worktree_repo):
    main = nested_worktree_repo["main"]
    core.acquire(main, reason="busy", duration=timedelta(minutes=5), session="session-b")
    result = run_hook(bash_payload("cat <(touch pwned)", main))
    assert result.returncode == 2


def test_bash_find_with_delete_denies(nested_worktree_repo):
    main = nested_worktree_repo["main"]
    core.acquire(main, reason="busy", duration=timedelta(minutes=5), session="session-b")
    result = run_hook(bash_payload("find . -delete", main))
    assert result.returncode == 2


def test_bash_find_plain_allows(nested_worktree_repo):
    main = nested_worktree_repo["main"]
    core.acquire(main, reason="busy", duration=timedelta(minutes=5), session="session-b")
    result = run_hook(bash_payload("find . -name x", main))
    assert result.returncode == 0


def test_bash_git_branch_mutation_denies(nested_worktree_repo):
    main = nested_worktree_repo["main"]
    core.acquire(main, reason="busy", duration=timedelta(minutes=5), session="session-b")
    result = run_hook(bash_payload("git branch -D x", main))
    assert result.returncode == 2


def test_bash_git_diff_output_flag_denies(nested_worktree_repo):
    main = nested_worktree_repo["main"]
    core.acquire(main, reason="busy", duration=timedelta(minutes=5), session="session-b")
    result = run_hook(bash_payload("git diff --output=out.txt", main))
    assert result.returncode == 2


def test_bash_tilde_path_token_under_foreign_lock_denies(nested_worktree_repo, tmp_path):
    sibling = nested_worktree_repo["sibling"]
    home = tmp_path / "home"
    locked = home / "lockedname"
    locked.mkdir(parents=True)
    core.acquire(locked, reason="busy", duration=timedelta(minutes=5), session="session-b")
    result = run_hook(
        bash_payload('echo x > "~/lockedname/out.txt"', sibling),
        {"HOME": str(home), "USERPROFILE": str(home)},
    )
    assert result.returncode == 2


def test_bash_home_var_path_token_under_foreign_lock_denies(nested_worktree_repo, tmp_path):
    sibling = nested_worktree_repo["sibling"]
    home = tmp_path / "home"
    locked = home / "lockedname"
    locked.mkdir(parents=True)
    core.acquire(locked, reason="busy", duration=timedelta(minutes=5), session="session-b")
    result = run_hook(
        bash_payload('echo x > "$HOME/lockedname/out.txt"', sibling),
        {"HOME": str(home), "USERPROFILE": str(home)},
    )
    assert result.returncode == 2


def test_bash_read_only_command_referencing_foreign_absolute_path_allows(
    nested_worktree_repo,
):
    main = nested_worktree_repo["main"]
    sibling = nested_worktree_repo["sibling"]
    core.acquire(main, reason="busy", duration=timedelta(minutes=5), session="session-b")
    result = run_hook(bash_payload(f'cat "{main / "README.md"}"', sibling))
    assert result.returncode == 0


def test_bash_read_only_git_log_referencing_foreign_absolute_path_allows(
    nested_worktree_repo,
):
    main = nested_worktree_repo["main"]
    sibling = nested_worktree_repo["sibling"]
    core.acquire(main, reason="busy", duration=timedelta(minutes=5), session="session-b")
    result = run_hook(bash_payload(f'git log "{main}"', sibling))
    assert result.returncode == 0


def test_git_config_write_allowed_when_nothing_is_locked(nested_worktree_repo):
    """An idle repository has nobody to coordinate with."""
    main = nested_worktree_repo["main"]
    result = run_hook(edit_payload(main / ".git" / "config", session_id="session-a"))
    assert result.returncode == 0


def test_git_config_write_allowed_to_the_main_worktree_holder(nested_worktree_repo):
    """Holding the main checkout means being in charge of the repository."""
    main = nested_worktree_repo["main"]
    core.acquire(main, reason="in charge", duration=timedelta(minutes=5), session="session-a")
    result = run_hook(edit_payload(main / ".git" / "config", session_id="session-a"))
    assert result.returncode == 0


def test_git_config_write_denied_when_another_session_holds_main(nested_worktree_repo):
    main = nested_worktree_repo["main"]
    core.acquire(main, reason="busy", duration=timedelta(minutes=5), session="session-b")
    result = run_hook(edit_payload(main / ".git" / "config", session_id="session-a"))
    assert result.returncode == 2


def test_git_config_write_denied_to_a_linked_worktree_holder(nested_worktree_repo):
    """A sibling worktree's lock does not confer repository-wide authority."""
    sibling = nested_worktree_repo["sibling"]
    core.acquire(sibling, reason="mine", duration=timedelta(minutes=5), session="session-a")
    main = nested_worktree_repo["main"]
    result = run_hook(edit_payload(main / ".git" / "config", session_id="session-a"))
    assert result.returncode == 2
    assert "claim that checkout" in result.stderr


def test_hand_editable_paths_reachable_by_the_main_holder(nested_worktree_repo):
    """Git offers no command for these, so they must stay hand-editable."""
    main = nested_worktree_repo["main"]
    core.acquire(main, reason="in charge", duration=timedelta(minutes=5), session="session-a")
    for target in (
        main / ".git" / "info" / "exclude",
        main / ".git" / "hooks" / "pre-commit",
    ):
        result = run_hook(edit_payload(target, session_id="session-a"))
        assert result.returncode == 0, f"{target} should be reachable"


def test_hand_editable_paths_reachable_when_repository_is_idle(nested_worktree_repo):
    main = nested_worktree_repo["main"]
    result = run_hook(edit_payload(main / ".git" / "info" / "exclude", session_id="session-a"))
    assert result.returncode == 0


def test_private_worktree_metadata_answers_to_the_main_checkout(nested_worktree_repo):
    sibling = nested_worktree_repo["sibling"]
    core.acquire(sibling, reason="mine", duration=timedelta(minutes=5), session="session-a")
    target = nested_worktree_repo["main"] / ".git" / "worktrees" / "sibling" / "HEAD"
    result = run_hook(edit_payload(target, session_id="session-a"))
    assert result.returncode == 2


def test_worktree_content_write_still_allowed_to_the_lock_holder(nested_worktree_repo):
    main = nested_worktree_repo["main"]
    core.acquire(main, reason="mine", duration=timedelta(minutes=5), session="session-a")
    result = run_hook(edit_payload(main / "README.md", session_id="session-a"))
    assert result.returncode == 0


def test_git_admin_denial_respects_warn_mode(nested_worktree_repo):
    main = nested_worktree_repo["main"]
    core.acquire(main, reason="busy", duration=timedelta(minutes=5), session="session-b")
    result = run_hook(
        edit_payload(main / ".git" / "config", session_id="session-a"),
        {"PROJECT_LOCK_ENFORCE": "warn"},
    )
    assert result.returncode == 0
