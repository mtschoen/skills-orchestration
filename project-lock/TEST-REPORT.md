Codex release-hook repair - 2026-10-01 UTC
=========================================

- Base: orchestration `34c6b2b`, branch `fix/codex-sandbox-release`.
- Mode: maintain test/coverage gate; record static-scan findings separately.
- Windows Python 3.13: **181 passed**, 0 failed, 0 skipped in 79.13 seconds.
- Coverage: **719/719 statements, 210/210 branches, 100%**.
- Ruff check and format check: passed. Markdownlint: 0 findings.
  Agent Skills validator 0.1.1: passed.
- Regression: correct ownership-token releases across sessions; incorrect tokens,
  alternate scripts/interpreters, shell chaining/substitution, extra options,
  malformed quoting, missing metadata, and absent locks do not earn the exception.
- Independent read-only review: no blockers. The CLI's transactional recheck
  protects replacement locks after a hook authorization snapshot.
- Native subagent regression: `CODEX_SESSION_ID` matches the hooks' session even
  when `CODEX_THREAD_ID` names a child. Both Codex identities suppress inherited
  Claude PID nomination; explicit identity overrides remain available.
- Installed smoke: captured token released the previously blocked synthetic lock.
  Actual `codex sandbox -P project-lock` lifecycle passed: Codex identity takes
  precedence over inherited Claude identity, competing acquire rejected, own edit
  allowed, foreign edit denied, renew succeeds, release clears marker and registry.
  Unrelated `.git/config` remained protected.
- Test environment: do not hold a home-directory lock over the real pytest temp
  root; existing ancestor-discovery tests intentionally inspect the filesystem and
  otherwise find that ambient lock. The earlier five failures disappeared after
  releasing the session's own home lock. No test assertions or gates were weakened.
- Aislop 0.16.0: one new false positive on the sibling `project_lock` import in
  `hooks/pre_tool_use.py` (the hook inserts its `scripts` directory into `sys.path`).
  No suppression added. Existing warnings: repeated `store_true`/`utf-8` literals,
  a wall-clock assertion in `tests/test_hook.py`, and legacy `telemetry: false`
  schema. This scan is not clean; its configuration was not changed.

Commands: `python -m pytest -q --tb=short -p no:cacheprovider`,
`python -m ruff check scripts tests hooks`,
`python -m ruff format --check scripts tests hooks`,
`uvx --from skills-ref==0.1.1 agentskills validate ../project-lock`, `aislop scan .`.

Historical report - 2026-08-06T01:10:00Z
--------------------------------------

Git:      main @ a4760e8 (pre-commit working tree)
Status:   PASS (Windows local, WSL Ubuntu-24.04, and ubuntu-latest CI)
Tests:    149 passed, 0 failed, 0 skipped
Coverage: 672/672 statements, 192/192 branches (100.0%) on scripts/project_lock,
          on both Windows and Linux
Lint:     ruff check: clean
          ruff format --check: clean
          markdownlint-cli2: 0 findings on tracked files
          agentskills validate (skills-ref 0.1.1): valid
          aislop ci: exit 0

Commands
--------

```bash
python -m pytest -q
ruff check scripts tests hooks
ruff format --check scripts tests hooks
npx --yes markdownlint-cli2
uvx --from skills-ref==0.1.1 agentskills validate
```

Coverage note
-------------

Coverage is 100% on **both** platforms as of 2026-08-06. It previously sat at
99.8% on Windows: the POSIX arm of `_temp_roots()` was gated directly on
`os.name != "nt"` and so was unreachable here, leaving the 100% gate
checkable only on Linux CI.

The gate could not be closed by patching `os.name` in a test, because
`pathlib` reads it to choose its flavour - forcing `"posix"` on Windows makes
the next `Path.resolve()` die with `UnsupportedOperation: cannot instantiate
'PosixPath' on your system`. The platform test now lives behind a named seam,
`core._is_windows()`, which tests patch instead. Both arms are exercised on
every host, so neither test depends on where the suite runs.

An earlier revision of this report recorded three failures and a second
uncovered line as permanent machine-local artifacts. They were caused by an
empty stray `~/.git` on the authoring host, which made `has_git_ancestor()`
true for the whole home tree and so disabled the scratch carve-out under
`%TEMP%`. That directory was deleted on 2026-08-05 and all three now pass.

Platform parity
---------------

A green Windows suite is not evidence of a green CI. The owner-liveness tests
faked `ctypes.get_last_error` with monkeypatch's default `raising=True`; that
name exists only on Windows builds of `ctypes`, so all five died on
ubuntu-latest and dropped coverage to 97, failing the 100% gate. CI was red
from the liveness commit onward while this report said PASS, because every
local run had the attribute. Anything patched onto a platform-conditional
standard-library name needs `raising=False`, and a report generated on one
platform should be read against the CI run for the same commit.

The cheap way to close that gap on a Windows host is WSL, which runs the real
Linux suite against the same checkout over `/mnt/c`:

```bash
wsl.exe -d Ubuntu-24.04 -- bash -lc \
  'cd /mnt/c/<path>/project-lock && ~/.venvs/pl/bin/python -m pytest -q'
```

It reproduced the five `ctypes` failures exactly, where faking the platform on
Windows (a plugin that deletes the attribute) produced two spurious extras.

Performance (chromium: 500,533 tracked files, 1.5 GB .git)
----------------------------------------------------------

Measured against `~/aislop-stress/chromium` and a tiny repo. Costs are
identical in both, so repository size does not enter any hook path: the git
commands used (`rev-parse`, `worktree list`) read small metadata files and
never touch the index or working tree.

| Path | Checks only | Whole hook process |
| --- | --- | --- |
| Ordinary content edit (the 99% case) | 1.3 ms | ~160 ms |
| `.git` administration edit (rare) | 32 ms | ~190 ms |

`is_git_admin_path` costs 0.00 ms on the content path: the `.git`-component
pre-filter returns before any subprocess. The ~160 ms is Python interpreter
startup (111 ms for a bare `python -c pass` on this host) plus imports, and is
unchanged by this feature.

Asking `rev-parse` for `--git-dir` and `--git-common-dir` in one invocation
costs the same as asking for either alone, which took the admin path from
three subprocesses to two (43 ms to 32 ms).

Smoke (SMOKE.md, live installed skill)
--------------------------------------

- Force-clear without `--reason` refused; exit 3.
- `check` on a lock with no nominated process: `owner pid: - (unknown)`.
- `acquire --owner-pid <live Win32 pid>` then `check`: `running`.
- Same lock after killing that process: `gone`, while `advice` still reads
  "wait, then check again" rather than declaring the lock free.
- `acquire --owner --owner-pid` after the `Claimant` grouping: all three
  claimant fields reach `owner.json` unchanged, and `creator_pid` stays
  distinct from `owner_pid`.
- Liveness against a real Win32 pid with no handle retained by the launcher:
  `running` while alive, `gone` after `kill -9`. Caveat observed while
  smoke-testing: if the launcher still holds an open handle to the exited
  process (a PowerShell `Start-Process -PassThru` object does), Windows keeps
  the pid openable and liveness keeps reporting `running`. That errs in the
  safe direction, since the pid also cannot be reused while the handle lives,
  so the lock is simply treated as still held.
- Temp-root detection after the `_is_windows()` seam, on both hosts: Windows
  reports only `%TEMP%` with no drive-relative `/tmp` enrolled; Linux reports
  `/tmp` and excludes `/home`.
- Git-admin ownership, all seven cases against real `git worktree` fixtures:
  idle repo allows `config`, `info/exclude` and `hooks/pre-commit`; holding the
  main checkout allows `config`; another session holding main denies; holding
  only a linked worktree denies; that worktree's own content still allows.

Markdownlint reports findings under `.superpowers/sdd/`, which is git-ignored
scaffolding (`.superpowers/sdd/.gitignore` contains `*`) and never present in
a CI checkout. Tracked markdown is clean.
