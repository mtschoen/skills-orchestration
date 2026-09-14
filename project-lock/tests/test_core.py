from __future__ import annotations

import json
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from project_lock import core


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    monkeypatch.setenv("PROJECT_LOCK_STATE_DIR", str(tmp_path / "state"))
    return root


def test_acquire_inspect_renew_release(repository: Path) -> None:
    metadata = core.acquire(
        repository,
        reason="quick fix",
        duration=timedelta(minutes=2),
        strategy="auto",
        owner="test-agent",
        session="session-1",
    )

    assert metadata["owner"] == "test-agent"
    assert metadata["session"] == "session-1"
    assert metadata["platform"]
    assert core.inspect(repository)["recommendation"] == "wait, then check again"
    assert core.list_locks()[0]["lock"]["lock_id"] == metadata["lock_id"]
    assert "/.agent-lock/" in (repository / ".git" / "info" / "exclude").read_text()

    renewed = core.renew(repository, lock_id=metadata["lock_id"], duration=timedelta(hours=1))
    assert renewed["updated_at"] >= metadata["updated_at"]
    assert core.inspect(repository)["recommendation"] == "use a separate worktree"
    assert core.release(repository, lock_id=metadata["lock_id"])
    assert not core.release(repository, force=True)
    assert core.list_locks() == []


def test_default_session_reads_recognized_environment_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert core.default_session() is None

    monkeypatch.setenv("PI_SESSION_ID", "pi-session")
    assert core.default_session() == "pi-session"

    # CLAUDE_CODE_SESSION_ID is earlier in the candidate list, so it wins when
    # both are set.
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude-session")
    assert core.default_session() == "claude-session"


def test_default_owner_pid_reads_recognized_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert core.default_owner_pid() is None

    monkeypatch.setenv("CLAUDE_PID", "27044")
    assert core.default_owner_pid() == 27044


def test_codex_thread_owns_lock_instead_of_inherited_claude_identity(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_THREAD_ID", "codex-thread")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-claude")
    monkeypatch.setenv("CLAUDE_PID", "4242")
    metadata = core.acquire(repository, reason="Codex session", duration=timedelta(minutes=1))
    assert metadata["session"] == "codex-thread"
    assert metadata["owner_pid"] is None


def test_explicit_identity_overrides_codex_environment(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_THREAD_ID", "codex-thread")
    metadata = core.acquire(
        repository,
        reason="Explicit session",
        duration=timedelta(minutes=1),
        session="explicit",
        owner_pid=9999,
    )
    assert metadata["session"] == "explicit"
    assert metadata["owner_pid"] == 9999


@pytest.mark.parametrize("value", ["not-a-number", "0", "-5", ""])
def test_default_owner_pid_rejects_unusable_values(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_PID", value)
    assert core.default_owner_pid() is None


def test_acquire_defaults_session_and_owner_pid_from_environment(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "env-session")
    monkeypatch.setenv("CLAUDE_PID", "4242")

    metadata = core.acquire(repository, reason="env defaults", duration=timedelta(minutes=1))

    assert metadata["session"] == "env-session"
    assert metadata["owner_pid"] == 4242


def test_acquire_explicit_session_and_owner_pid_win_over_environment(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "env-session")
    monkeypatch.setenv("CLAUDE_PID", "4242")

    metadata = core.acquire(
        repository,
        reason="explicit wins",
        duration=timedelta(minutes=1),
        session="explicit-session",
        owner_pid=9999,
    )

    assert metadata["session"] == "explicit-session"
    assert metadata["owner_pid"] == 9999


def test_acquire_without_session_or_environment_leaves_session_null(repository: Path) -> None:
    metadata = core.acquire(repository, reason="no session", duration=timedelta(minutes=1))

    assert metadata["session"] is None
    assert metadata["owner_pid"] is None


def test_explicit_strategies_and_overdue(repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    metadata = core.acquire(
        repository,
        reason="main session",
        duration=timedelta(hours=1),
        strategy="wait",
    )
    assert core.inspect(repository)["recommendation"] == "wait for this worktree"

    core.release(repository, lock_id=metadata["lock_id"])
    metadata = core.acquire(
        repository,
        reason="main session",
        duration=timedelta(hours=1),
        strategy="worktree",
    )
    assert core.inspect(repository)["recommendation"] == "use a separate worktree"

    future = core.parse_time(metadata["expected_until"]) + timedelta(seconds=1)
    monkeypatch.setattr(core, "utc_now", lambda: future)
    status = core.inspect(repository)
    assert status["overdue"]
    assert status["recommendation"].startswith("overdue:")


def test_conflict_and_ownership_errors(repository: Path) -> None:
    metadata = core.acquire(
        repository,
        reason="first",
        duration=timedelta(minutes=1),
    )
    with pytest.raises(core.LockConflict) as conflict:
        core.acquire(repository, reason="second", duration=timedelta(minutes=1))
    assert conflict.value.status["lock"]["reason"] == "first"

    with pytest.raises(core.LockOwnershipError, match="does not match"):
        core.renew(repository, lock_id="wrong", duration=timedelta(minutes=1))
    with pytest.raises(core.LockOwnershipError, match="required"):
        core.release(repository)
    with pytest.raises(core.LockOwnershipError, match="does not match"):
        core.release(repository, lock_id="wrong")
    assert core.release(
        repository,
        force=True,
        expect_lock_id=metadata["lock_id"],
        reason="owner verified abandoned",
    )
    assert metadata["lock_id"]


def test_corrupt_metadata_requires_force(repository: Path) -> None:
    marker = repository / core.MARKER_DIRECTORY_NAME
    marker.mkdir()
    metadata_file = marker / core.METADATA_FILE_NAME
    metadata_file.write_text('{"version": 1, "lock_id": "partial"}')
    status = core.inspect(repository)
    assert status["locked"]
    assert status["lock"]["owner"] == "unknown"

    with pytest.raises(core.LockOwnershipError, match="metadata is unavailable"):
        core.release(repository)
    unexpected = marker / "unexpected"
    unexpected.write_text("x")
    with pytest.raises(core.LockOwnershipError, match="unexpected"):
        core.release(repository, force=True, reason="metadata is corrupt")
    assert metadata_file.exists()
    unexpected.unlink()
    # Unreadable metadata carries no lock id to compare, so the reason is the
    # whole gate; --expect-lock-id is only demanded for a readable lock.
    assert core.release(repository, force=True, reason="metadata is corrupt")


def test_non_git_root_and_file_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROJECT_LOCK_STATE_DIR", str(tmp_path / "state"))
    project = tmp_path / "plain"
    project.mkdir()
    file_path = project / "file.txt"
    file_path.write_text("content")

    metadata = core.acquire(file_path, reason="plain", duration=timedelta(seconds=1))
    assert Path(metadata["root"]) == core.canonical_path(project)
    assert core.release(project, lock_id=metadata["lock_id"])
    with pytest.raises(FileNotFoundError):
        core.inspect(tmp_path / "missing")


def test_registry_cleanup(repository: Path) -> None:
    registry_directory = core.state_directory() / "locks"
    registry_directory.mkdir(parents=True)
    corrupt = registry_directory / "corrupt.json"
    corrupt.write_text("not-json")
    missing = registry_directory / "missing.json"
    missing.write_text(json.dumps({"root": str(repository.parent / "gone")}))
    free = registry_directory / "free.json"
    free.write_text(json.dumps({"root": str(repository)}))

    assert core.list_locks() == []
    assert corrupt.exists()
    assert not missing.exists()
    assert not free.exists()


def test_helpers_and_failure_cleanup(repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert core.format_time(datetime(2026, 1, 1, tzinfo=UTC)) == "2026-01-01T00:00:00Z"
    assert core.default_owner()
    assert core.current_branch(repository) == "main"

    monkeypatch.setattr(core, "atomic_write_json", lambda *_: (_ for _ in ()).throw(OSError("no")))
    with pytest.raises(OSError, match="no"):
        core.acquire(repository, reason="failure", duration=timedelta(seconds=1))
    assert not (repository / core.MARKER_DIRECTORY_NAME).exists()


def test_state_directory_fallbacks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROJECT_LOCK_STATE_DIR", raising=False)
    monkeypatch.setattr(core.platform, "system", lambda: "Windows")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert core.state_directory() == tmp_path / "local" / "project-lock"
    monkeypatch.delenv("LOCALAPPDATA")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "windows-fallback"))
    assert core.state_directory() == tmp_path / "windows-fallback" / "project-lock"
    monkeypatch.setattr(core.platform, "system", lambda: "Linux")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert core.state_directory() == tmp_path / "project-lock"
    monkeypatch.delenv("XDG_STATE_HOME")
    monkeypatch.setattr(core.Path, "home", lambda: tmp_path)
    assert core.state_directory() == tmp_path / ".local" / "state" / "project-lock"


def test_valid_metadata_rejects_invalid_shapes(repository: Path) -> None:
    metadata = core.build_metadata(
        repository,
        reason="test",
        duration=timedelta(minutes=1),
        strategy="auto",
        claimant=core.Claimant(owner="owner"),
    )
    assert core.valid_metadata(metadata) is metadata
    assert core.valid_metadata(None) is None
    for change in (
        {"version": 0},
        {"owner": ""},
        {"owner": 1},
        {"strategy": "invalid"},
        {"expected_until": "not-a-time"},
        {"expected_until": "2026-01-01T00:00:00"},
    ):
        candidate = metadata | change
        assert core.valid_metadata(candidate) is None


def test_mutations_are_serialized(repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = core.acquire(repository, reason="first", duration=timedelta(minutes=1))
    renewal_paused = threading.Event()
    continue_renewal = threading.Event()
    original_write = core.atomic_write_json

    def pausing_write(path: Path, payload: dict) -> None:
        if threading.current_thread().name == "renew" and path == core.metadata_path(repository):
            renewal_paused.set()
            assert continue_renewal.wait(timeout=5)
        original_write(path, payload)

    monkeypatch.setattr(core, "atomic_write_json", pausing_write)
    renew_thread = threading.Thread(
        name="renew",
        target=core.renew,
        kwargs={"path": repository, "lock_id": first["lock_id"], "duration": timedelta(minutes=2)},
    )
    replacement: dict = {}

    def replace_lock() -> None:
        core.release(repository, lock_id=first["lock_id"])
        replacement.update(core.acquire(repository, reason="second", duration=timedelta(minutes=1)))

    replace_thread = threading.Thread(name="replace", target=replace_lock)
    renew_thread.start()
    assert renewal_paused.wait(timeout=5)
    replace_thread.start()
    assert replace_thread.is_alive()
    continue_renewal.set()
    renew_thread.join(timeout=5)
    replace_thread.join(timeout=5)
    assert not renew_thread.is_alive()
    assert not replace_thread.is_alive()
    assert core.inspect(repository)["lock"]["lock_id"] == replacement["lock_id"]


def test_release_failure_preserves_metadata(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metadata = core.acquire(repository, reason="owner evidence", duration=timedelta(minutes=1))
    original_remove_directory = core.Path.rmdir

    def fail_for_marker(path: Path) -> None:
        if path == core.marker_directory(repository):
            raise PermissionError("simulated")
        original_remove_directory(path)

    monkeypatch.setattr(core.Path, "rmdir", fail_for_marker)
    with pytest.raises(core.LockOwnershipError, match="could not remove"):
        core.release(repository, lock_id=metadata["lock_id"])
    assert core.inspect(repository)["lock"]["owner"] == metadata["owner"]

    monkeypatch.setattr(core.Path, "rmdir", original_remove_directory)
    assert core.release(repository, lock_id=metadata["lock_id"])
    core.marker_directory(repository).mkdir()
    monkeypatch.setattr(core.Path, "rmdir", fail_for_marker)
    with pytest.raises(core.LockOwnershipError, match="could not remove"):
        core.release(repository, force=True, reason="owner verified abandoned")


def test_ignore_and_missing_renew_paths(repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exclude = repository / ".git" / "info" / "exclude"
    exclude.write_text("existing-without-newline")
    core.ensure_git_ignore(repository)
    first = exclude.read_text()
    assert first == "existing-without-newline\n/.agent-lock/\n"
    core.ensure_git_ignore(repository)
    assert exclude.read_text() == first

    monkeypatch.setattr(core, "run_git", lambda *_: str(repository / ".git"))
    core.ensure_git_ignore(repository)
    with pytest.raises(core.LockOwnershipError, match="not locked"):
        core.renew(repository, lock_id="none", duration=timedelta(seconds=1))


def test_governing_lock_finds_ancestor_within_worktree(nested_worktree_repo):
    main = nested_worktree_repo["main"]
    core.acquire(main, reason="r", duration=timedelta(minutes=5))
    governed = core.governing_lock(main / "src" / "new_file.py")
    assert governed is not None
    assert governed["root"] == str(core.canonical_path(main))
    assert governed["lock"]["reason"] == "r"


def test_governing_lock_stops_at_nested_worktree_boundary(nested_worktree_repo):
    main = nested_worktree_repo["main"]
    nested = nested_worktree_repo["nested"]
    core.acquire(main, reason="r", duration=timedelta(minutes=5))
    assert core.governing_lock(nested / "inner.py") is None


def test_governing_lock_sees_nested_lock_not_parent(nested_worktree_repo):
    main = nested_worktree_repo["main"]
    nested = nested_worktree_repo["nested"]
    core.acquire(main, reason="parent", duration=timedelta(minutes=5))
    core.acquire(nested, reason="child", duration=timedelta(minutes=5))
    governed = core.governing_lock(nested / "inner.py")
    assert governed["lock"]["reason"] == "child"


def test_governing_lock_damaged_marker_reports_none_lock(tmp_path):
    project = tmp_path / "plain"
    project.mkdir()
    (project / ".agent-lock").mkdir()
    governed = core.governing_lock(project / "file.txt")
    assert governed == {"root": str(core.canonical_path(project)), "lock": None}


def test_governing_lock_no_marker_returns_none(nested_worktree_repo):
    assert core.governing_lock(nested_worktree_repo["sibling"] / "x.py") is None


def test_nearest_worktree_root(nested_worktree_repo, tmp_path):
    nested = nested_worktree_repo["nested"]
    assert core.nearest_worktree_root(nested / "deep" / "x.py") == core.canonical_path(nested)
    plain = tmp_path / "plain2"
    plain.mkdir()
    assert core.nearest_worktree_root(plain / "x.py") == core.canonical_path(plain)


def test_deepest_existing_directory_resolves_existing_file(nested_worktree_repo):
    main = nested_worktree_repo["main"]
    assert core.deepest_existing_directory(main / "README.md") == core.canonical_path(main)


def test_deepest_existing_directory_stops_at_filesystem_root(tmp_path, monkeypatch):
    monkeypatch.setattr(core.Path, "exists", lambda self: False)
    root = Path(tmp_path.anchor)
    result = core.deepest_existing_directory(root / "definitely" / "not" / "real" / "path")
    assert result == core.canonical_path(root)


def test_governing_lock_climbs_to_root_without_marker_or_git(tmp_path, monkeypatch):
    monkeypatch.setattr(core.Path, "exists", lambda self: str(self) == str(tmp_path / "plain3"))
    assert core.governing_lock(tmp_path / "plain3" / "x.py") is None


def test_has_git_ancestor_true_inside_repo(nested_worktree_repo):
    main = nested_worktree_repo["main"]
    assert core.has_git_ancestor(main / "deep" / "file.py") is True


def test_has_git_ancestor_false_without_git(tmp_path, monkeypatch):
    monkeypatch.setattr(core.Path, "exists", lambda self: str(self) == str(tmp_path / "plain4"))
    assert core.has_git_ancestor(tmp_path / "plain4" / "x.py") is False


def test_temp_roots_includes_tmpdir_env_and_dedupes_gettempdir(tmp_path, monkeypatch):
    monkeypatch.setattr(core.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    roots = core._temp_roots()
    assert roots.count(core.canonical_path(tmp_path)) == 1


def test_temp_roots_skips_unset_env_and_missing_candidates(tmp_path, monkeypatch):
    missing = tmp_path / "does-not-exist"
    monkeypatch.setattr(core.tempfile, "gettempdir", lambda: str(missing))
    monkeypatch.delenv("TMPDIR", raising=False)
    roots = core._temp_roots()
    assert core.canonical_path(missing) not in roots


def test_temp_roots_probes_posix_literals_off_windows(tmp_path, monkeypatch):
    # The mirror of the test below. Both run everywhere: the platform seam is
    # patched, so neither branch depends on the host the suite runs on.
    monkeypatch.setattr(core, "_is_windows", lambda: False)
    monkeypatch.setattr(core.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.delenv("TMPDIR", raising=False)
    posix_literals = [Path("/tmp"), Path("/private/tmp"), Path("/var/folders")]
    probed: list[Path] = []
    original_exists = Path.exists

    def tracking_exists(self):
        if self in posix_literals:
            probed.append(self)
            # Report them absent so the result stays identical on every host,
            # including Windows where "/tmp" resolves against the cwd drive.
            return False
        return original_exists(self)

    monkeypatch.setattr(core.Path, "exists", tracking_exists)
    roots = core._temp_roots()
    assert probed == posix_literals
    assert roots == [core.canonical_path(tmp_path)]


def test_temp_roots_skips_posix_literals_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_is_windows", lambda: True)
    monkeypatch.setattr(core.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.delenv("TMPDIR", raising=False)
    posix_literals = [Path("/tmp"), Path("/private/tmp"), Path("/var/folders")]
    probed: list[Path] = []
    original_exists = Path.exists

    def tracking_exists(self):
        if self in posix_literals:
            probed.append(self)
            return True
        return original_exists(self)

    monkeypatch.setattr(core.Path, "exists", tracking_exists)
    roots = core._temp_roots()
    assert probed == []
    assert all(root not in posix_literals for root in roots)


def test_is_under_temp_dir_true_when_target_is_root(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_temp_roots", lambda: [core.canonical_path(tmp_path)])
    assert core.is_under_temp_dir(tmp_path) is True


def test_is_under_temp_dir_true_when_root_is_ancestor(tmp_path, monkeypatch):
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.setattr(core, "_temp_roots", lambda: [core.canonical_path(tmp_path)])
    assert core.is_under_temp_dir(child / "file.txt") is True


def test_is_under_temp_dir_false_when_root_present_but_unrelated(tmp_path, monkeypatch):
    other_root = tmp_path / "other-root"
    other_root.mkdir()
    unrelated = tmp_path / "elsewhere"
    unrelated.mkdir()
    monkeypatch.setattr(core, "_temp_roots", lambda: [core.canonical_path(other_root)])
    assert core.is_under_temp_dir(unrelated / "file.txt") is False


def test_is_under_temp_dir_false_without_recognized_root(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_temp_roots", list)
    assert core.is_under_temp_dir(tmp_path / "x") is False


def test_related_locks_reports_descendant_and_ancestor(nested_worktree_repo):
    main = nested_worktree_repo["main"]
    nested = nested_worktree_repo["nested"]
    core.acquire(nested, reason="child", duration=timedelta(minutes=5))
    related = core.related_locks(main)
    assert [entry["lock"]["reason"] for entry in related["descendants"]] == ["child"]
    related_from_nested = core.related_locks(nested)
    assert related_from_nested["descendants"] == []
    core.acquire(main, reason="parent", duration=timedelta(minutes=5))
    related_from_nested = core.related_locks(nested)
    assert [e["lock"]["reason"] for e in related_from_nested["ancestors"]] == ["parent"]


def test_inspect_includes_related(nested_worktree_repo):
    main = nested_worktree_repo["main"]
    nested = nested_worktree_repo["nested"]
    core.acquire(nested, reason="child", duration=timedelta(minutes=5))
    status = core.inspect(main)
    assert status["related"]["descendants"][0]["root"] == str(core.canonical_path(nested))


def test_related_locks_skips_invalid_and_stale_registry_entries(nested_worktree_repo):
    main = nested_worktree_repo["main"]
    nested = nested_worktree_repo["nested"]
    registry_directory = core.state_directory() / "locks"
    registry_directory.mkdir(parents=True)
    corrupt = registry_directory / "corrupt.json"
    corrupt.write_text("not-json")
    stale_metadata = core.build_metadata(
        core.canonical_path(nested),
        reason="stale",
        duration=timedelta(minutes=5),
        strategy="auto",
        claimant=core.Claimant(owner="ghost"),
    )
    stale = registry_directory / "stale.json"
    stale.write_text(json.dumps(stale_metadata))
    related = core.related_locks(main)
    assert related["descendants"] == []


def test_inspect_damaged_metadata_includes_related(nested_worktree_repo):
    main = nested_worktree_repo["main"]
    nested = nested_worktree_repo["nested"]
    core.acquire(nested, reason="child", duration=timedelta(minutes=5))
    (main / core.MARKER_DIRECTORY_NAME).mkdir()
    status = core.inspect(main)
    assert status["lock"]["owner"] == "unknown"
    assert status["related"]["descendants"][0]["lock"]["reason"] == "child"
