"""Tests for path, SSH, worktree, and result helpers."""

from __future__ import annotations

import base64
import json
import subprocess
import unittest
from unittest.mock import patch

from tests.agent_remote_test_support import agent_remote, completed_process


class RunResultTests(unittest.TestCase):
    def test_serializes_success(self) -> None:
        result = agent_remote.RunResult(
            host="worker@example",
            branch="agent-remote/task",
            worktree_path="/srv/worktree",
            parent_commit="parent",
            new_commit="child",
            files_changed=["result.txt"],
            agent_exit_code=0,
            stdout_tail="output",
            stderr_tail="",
            cleanup_command="cleanup",
        )

        payload = json.loads(result.to_json())

        self.assertTrue(result.success)
        self.assertEqual(payload["agent_exit_code"], 0)
        self.assertEqual(payload["new_commit"], "child")

    def test_nonzero_exit_is_unsuccessful(self) -> None:
        result = agent_remote.RunResult(
            "host", "branch", "/worktree", "parent", None, [], 7, "", "", "cleanup"
        )

        self.assertFalse(result.success)


class PathTests(unittest.TestCase):
    def test_remote_path_mangling_only_applies_on_windows(self) -> None:
        with patch.object(agent_remote.sys, "platform", "win32"):
            self.assertEqual(agent_remote.rpath("/srv/project"), "//srv/project")
            self.assertEqual(agent_remote.rpath("//srv/project"), "//srv/project")
            self.assertEqual(agent_remote.rpath("relative/path"), "relative/path")
        with patch.object(agent_remote.sys, "platform", "linux"):
            self.assertEqual(agent_remote.rpath("/srv/project"), "/srv/project")

    def test_quoted_remote_path_uses_mangled_path_on_posix(self) -> None:
        with patch.object(agent_remote, "rpath", return_value="path with spaces"):
            self.assertEqual(
                agent_remote.qrp("ignored", os_type="posix"), "'path with spaces'"
            )

    def test_quoted_remote_path_uses_double_quotes_on_windows(self) -> None:
        self.assertEqual(
            agent_remote.qrp("C:\\Program Files\\Repo", os_type="windows"),
            '"C:\\Program Files\\Repo"',
        )

    def test_unmangles_known_git_for_windows_prefixes(self) -> None:
        with patch.object(agent_remote.sys, "platform", "win32"):
            self.assertEqual(
                agent_remote.unmangle_msys_path(
                    "C:/Program Files/Git/home/user/project"
                ),
                "/home/user/project",
            )
            self.assertEqual(
                agent_remote.unmangle_msys_path(
                    "C:\\Program Files (x86)\\Git\\home\\user\\project"
                ),
                "/home/user/project",
            )
            self.assertEqual(
                agent_remote.unmangle_msys_path("D:/project"), "D:/project"
            )
        with patch.object(agent_remote.sys, "platform", "linux"):
            self.assertEqual(
                agent_remote.unmangle_msys_path("C:/Program Files/Git/home/user"),
                "C:/Program Files/Git/home/user",
            )

    def test_computes_worktree_path(self) -> None:
        self.assertEqual(
            agent_remote.compute_worktree_path(
                "/srv/project/", "agent-remote/topic/branch"
            ),
            "/srv/agent-remote-worktrees/agent-remote_topic_branch",
        )
        self.assertEqual(
            agent_remote.compute_worktree_path(
                "C:\\Users\\user\\project",
                "agent-remote/topic/branch",
                os_type="windows",
            ),
            "C:\\Users\\user\\agent-remote-worktrees\\agent-remote_topic_branch",
        )

    def test_computes_worktree_path_preserves_backslash_in_posix_path(self) -> None:
        self.assertEqual(
            agent_remote.compute_worktree_path(
                "/srv/a\\b/project",
                "agent-remote/task",
                os_type="posix",
            ),
            "/srv/a\\b/agent-remote-worktrees/agent-remote_task",
        )


class DetectOsTests(unittest.TestCase):
    def setUp(self) -> None:
        agent_remote.clear_host_os_cache()

    def test_detects_posix_from_uname_output(self) -> None:
        with patch.object(
            agent_remote,
            "ssh_raw_run",
            return_value=completed_process(0, stdout="Linux\n"),
        ) as raw_run:
            os_type = agent_remote.detect_remote_os("user@llamabox")

        self.assertEqual(os_type, "posix")
        self.assertEqual(raw_run.call_count, 1)
        self.assertEqual(raw_run.call_args.args[1], "uname -s")
        # Second call reads from cache without running ssh_raw_run again
        self.assertEqual(agent_remote.detect_remote_os("user@llamabox"), "posix")
        self.assertEqual(raw_run.call_count, 1)

    def test_detects_posix_wsl_from_login_shell_without_cmd_exe(self) -> None:
        # WSL reports Linux via login shell; verify probe runs uname -s and never cmd.exe
        with patch.object(
            agent_remote,
            "ssh_raw_run",
            return_value=completed_process(0, stdout="Linux\n"),
        ) as raw_run:
            os_type = agent_remote.detect_remote_os("user@wsl-box")

        self.assertEqual(os_type, "posix")
        self.assertNotIn("cmd.exe", raw_run.call_args.args[1])

    def test_detects_windows_via_powershell_probe(self) -> None:
        with patch.object(
            agent_remote,
            "ssh_raw_run",
            side_effect=[
                completed_process(127, stderr="uname: command not found\n"),
                completed_process(0, stdout="Windows_NT\r\n"),
            ],
        ) as raw_run:
            os_type = agent_remote.detect_remote_os("mtsch@chonkers")

        self.assertEqual(os_type, "windows")
        self.assertEqual(raw_run.call_count, 2)
        self.assertEqual(raw_run.call_args_list[0].args[1], "uname -s")
        powershell_command = raw_run.call_args_list[1].args[1]
        self.assertIn("powershell -NoProfile -EncodedCommand", powershell_command)
        self.assertNotIn("$", powershell_command)
        self.assertNotIn('"', powershell_command)
        base64_payload = powershell_command.split("-EncodedCommand ")[1].strip()
        decoded_script = base64.b64decode(base64_payload).decode("utf-16-le")
        self.assertEqual(decoded_script, "$env:OS")
        # Second call reads from cache
        self.assertEqual(agent_remote.detect_remote_os("mtsch@chonkers"), "windows")
        self.assertEqual(raw_run.call_count, 2)

    def test_detects_windows_from_ver_fallback(self) -> None:
        ver_output = "Microsoft Windows [Version 10.0.26200.8894]\r\n"
        with patch.object(
            agent_remote,
            "ssh_raw_run",
            side_effect=[
                completed_process(127, stderr="uname: command not found\n"),
                completed_process(1, stderr="powershell: command not found\n"),
                completed_process(0, stdout=ver_output),
            ],
        ) as raw_run:
            os_type = agent_remote.detect_remote_os("mtsch@cmd-only")

        self.assertEqual(os_type, "windows")
        self.assertEqual(raw_run.call_count, 3)

    def test_detect_remote_os_raises_on_inconclusive_response_and_does_not_cache(
        self,
    ) -> None:
        scenarios = [
            completed_process(0, stdout=""),
            completed_process(1, stderr="Access is denied.\n"),
            completed_process(127, stderr="not found\n"),
        ]
        for process in scenarios:
            with self.subTest(stdout=process.stdout, stderr=process.stderr):
                with patch.object(
                    agent_remote,
                    "ssh_raw_run",
                    return_value=process,
                ):
                    with self.assertRaisesRegex(
                        RuntimeError, "Could not determine remote OS"
                    ):
                        agent_remote.detect_remote_os("inconclusive@host")
                self.assertIsNone(agent_remote.get_cached_host_os("inconclusive@host"))

    def test_detect_remote_os_raises_on_ssh_exit_255_and_does_not_cache(self) -> None:
        with patch.object(
            agent_remote,
            "ssh_raw_run",
            return_value=completed_process(255, stderr="ssh: Connection refused\n"),
        ):
            with self.assertRaisesRegex(RuntimeError, "SSH connection failed"):
                agent_remote.detect_remote_os("offline@host")

        self.assertIsNone(agent_remote.get_cached_host_os("offline@host"))

    def test_detect_remote_os_raises_on_timeout_and_does_not_cache(self) -> None:
        with patch.object(
            agent_remote,
            "ssh_raw_run",
            side_effect=subprocess.TimeoutExpired("ssh", 15),
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                agent_remote.detect_remote_os("hanging@host")

        self.assertIsNone(agent_remote.get_cached_host_os("hanging@host"))

    def test_respects_os_override_and_env_var(self) -> None:
        self.assertEqual(
            agent_remote.detect_remote_os("host1", os_override="windows"), "windows"
        )
        with patch.dict(agent_remote.os.environ, {"REMOTE_AGENT_OS": "posix"}):
            self.assertEqual(agent_remote.detect_remote_os("host2"), "posix")


class SshTests(unittest.TestCase):
    def setUp(self) -> None:
        agent_remote.clear_host_os_cache()

    def test_ssh_run_closes_stdin_and_builds_login_shell_command_on_posix(self) -> None:
        with (
            patch.object(agent_remote.sys, "platform", "linux"),
            patch.object(
                agent_remote.subprocess, "run", return_value=completed_process()
            ) as run,
        ):
            result = agent_remote.ssh_run(
                "worker@example", "whoami", timeout=12, os_type="posix"
            )

        self.assertEqual(result.returncode, 0)
        positional_arguments, keyword_arguments = run.call_args
        self.assertEqual(
            positional_arguments[0][:4],
            ["ssh", "-o", "BatchMode=yes", "worker@example"],
        )
        self.assertIn("bash -lc", positional_arguments[0][4])
        self.assertIn(agent_remote.REMOTE_PATH_PREFIX, positional_arguments[0][4])
        self.assertEqual(keyword_arguments["stdin"], subprocess.DEVNULL)
        self.assertEqual(keyword_arguments["timeout"], 12)
        self.assertEqual(keyword_arguments["encoding"], "utf-8")
        self.assertEqual(keyword_arguments["env"]["MSYS_NO_PATHCONV"], "1")

    def test_ssh_run_on_windows_remote_runs_native_command_without_bash_wrapper(
        self,
    ) -> None:
        with (
            patch.object(agent_remote.sys, "platform", "linux"),
            patch.object(
                agent_remote.subprocess, "run", return_value=completed_process()
            ) as run,
        ):
            result = agent_remote.ssh_run(
                "worker@example", "echo %COMSPEC% & ver", timeout=12, os_type="windows"
            )

        self.assertEqual(result.returncode, 0)
        positional_arguments, keyword_arguments = run.call_args
        self.assertEqual(
            positional_arguments[0],
            ["ssh", "-o", "BatchMode=yes", "worker@example", "echo %COMSPEC% & ver"],
        )
        self.assertNotIn("bash -lc", positional_arguments[0][4])

    def test_ssh_run_passes_input_and_windows_creation_flag(self) -> None:
        with (
            patch.object(agent_remote.sys, "platform", "win32"),
            patch.object(
                agent_remote.subprocess, "CREATE_NO_WINDOW", 134_217_728, create=True
            ),
            patch.object(
                agent_remote.subprocess, "run", return_value=completed_process()
            ) as run,
        ):
            agent_remote.ssh_run("host", "cat", input_text="content", os_type="posix")

        keyword_arguments = run.call_args.kwargs
        self.assertEqual(keyword_arguments["input"], "content")
        self.assertNotIn("stdin", keyword_arguments)
        self.assertEqual(keyword_arguments["creationflags"], 134_217_728)

    def test_put_file_creates_parent_and_streams_content_on_posix(self) -> None:
        with (
            patch.object(agent_remote.sys, "platform", "linux"),
            patch.object(
                agent_remote, "ssh_run", return_value=completed_process()
            ) as ssh_run,
        ):
            agent_remote.ssh_put_file(
                "host", "/srv/worktree/file.txt", "contents", os_type="posix"
            )

        remote_command = ssh_run.call_args.args[1]
        self.assertIn("mkdir -p /srv/worktree", remote_command)
        self.assertIn("cat > /srv/worktree/file.txt", remote_command)
        self.assertEqual(ssh_run.call_args.kwargs["input_text"], "contents")

    def test_put_file_uses_powershell_base64_on_windows(self) -> None:
        with (
            patch.object(agent_remote.sys, "platform", "linux"),
            patch.object(
                agent_remote, "ssh_run", return_value=completed_process()
            ) as ssh_run,
        ):
            agent_remote.ssh_put_file(
                "host", "C:/srv/worktree/file.txt", "contents", os_type="windows"
            )

        remote_command = ssh_run.call_args.args[1]
        self.assertIn("powershell -NoProfile -EncodedCommand", remote_command)
        b64_part = remote_command.split()[-1]
        decoded_script = base64.b64decode(b64_part).decode("utf-16-le")
        self.assertIn("FromBase64String", decoded_script)
        self.assertIn("WriteAllBytes", decoded_script)
        expected_b64 = base64.b64encode(b"contents").decode("ascii")
        self.assertEqual(ssh_run.call_args.kwargs["input_text"], expected_b64)

    def test_put_file_streams_large_payload_over_stdin_without_bloating_cli_on_windows(
        self,
    ) -> None:
        large_content = "A" * 10_000
        with (
            patch.object(agent_remote.sys, "platform", "linux"),
            patch.object(
                agent_remote, "ssh_run", return_value=completed_process()
            ) as ssh_run,
        ):
            agent_remote.ssh_put_file(
                "host",
                "C:/srv/worktree/.agent-prompt.txt",
                large_content,
                os_type="windows",
            )

        remote_command = ssh_run.call_args.args[1]
        # Command line stays compact and well within cmd.exe 8191 limit
        self.assertLess(len(remote_command), 2000)
        self.assertNotIn("AAAA", remote_command)
        expected_b64 = base64.b64encode(large_content.encode("utf-8")).decode("ascii")
        self.assertEqual(ssh_run.call_args.kwargs["input_text"], expected_b64)

    def test_put_file_reports_remote_failure(self) -> None:
        with (
            patch.object(
                agent_remote,
                "ssh_run",
                return_value=completed_process(5, stderr="permission denied"),
            ),
            self.assertRaisesRegex(RuntimeError, "permission denied"),
        ):
            agent_remote.ssh_put_file("host", "/srv/file", "contents", os_type="posix")

    def test_ssh_check_returns_stdout_or_raises_with_context(self) -> None:
        with patch.object(
            agent_remote, "ssh_run", return_value=completed_process(stdout="ok\n")
        ):
            self.assertEqual(
                agent_remote.ssh_check("host", "command", os_type="posix"), "ok\n"
            )
        with (
            patch.object(
                agent_remote,
                "ssh_run",
                return_value=completed_process(9, stderr="failed"),
            ),
            self.assertRaisesRegex(RuntimeError, r"\(collect state\)"),
        ):
            agent_remote.ssh_check(
                "host", "command", error_context="collect state", os_type="posix"
            )


class WorktreeTests(unittest.TestCase):
    def setUp(self) -> None:
        agent_remote.clear_host_os_cache()

    def test_reuses_existing_worktree_and_reads_parent(self) -> None:
        listing = (
            "worktree /srv/other\nHEAD abc\nbranch refs/heads/other\n\n"
            "worktree /srv/existing\nHEAD def\nbranch refs/heads/agent-remote/task\n"
        )
        with patch.object(
            agent_remote, "ssh_check", side_effect=[listing, "parent-sha\n"]
        ) as ssh_check:
            result = agent_remote.ensure_worktree(
                "host", "/srv/project", "agent-remote/task", os_type="posix"
            )

        self.assertEqual(result, ("/srv/existing", "parent-sha"))
        self.assertEqual(ssh_check.call_count, 2)

    def test_creates_missing_worktree_from_repository_head_on_posix(self) -> None:
        with patch.object(
            agent_remote,
            "ssh_check",
            side_effect=[
                "worktree /srv/project\nHEAD abc\nbranch refs/heads/main\n",
                "abc\n",
                "",
            ],
        ) as ssh_check:
            result = agent_remote.ensure_worktree(
                "host", "/srv/project", "agent-remote/task", os_type="posix"
            )

        self.assertEqual(
            result,
            ("/srv/agent-remote-worktrees/agent-remote_task", "abc"),
        )
        self.assertIn(
            "worktree add -b agent-remote/task", ssh_check.call_args_list[2].args[1]
        )

    def test_creates_missing_worktree_on_windows(self) -> None:
        with patch.object(
            agent_remote,
            "ssh_check",
            side_effect=[
                "worktree C:/srv/project\nHEAD abc\nbranch refs/heads/main\n",
                "abc\n",
                "",
            ],
        ) as ssh_check:
            result = agent_remote.ensure_worktree(
                "host", "C:/srv/project", "agent-remote/task", os_type="windows"
            )

        self.assertEqual(
            result,
            ("C:/srv/agent-remote-worktrees/agent-remote_task", "abc"),
        )
        add_command = ssh_check.call_args_list[2].args[1]
        self.assertIn('worktree add -b "agent-remote/task"', add_command)
        self.assertIn('"C:/srv/agent-remote-worktrees/agent-remote_task"', add_command)

    def test_seed_settings_writes_expected_json(self) -> None:
        with patch.object(agent_remote, "ssh_put_file") as put_file:
            agent_remote.seed_settings(
                "host", "/srv/worktree", ["Read", "Write"], os_type="posix"
            )

        self.assertEqual(
            put_file.call_args.args[:2],
            ("host", "/srv/worktree/.claude/settings.local.json"),
        )
        self.assertEqual(
            json.loads(put_file.call_args.args[2]),
            {"permissions": {"allow": ["Read", "Write"]}},
        )


class ResultCollectionTests(unittest.TestCase):
    def setUp(self) -> None:
        agent_remote.clear_host_os_cache()

    def test_collects_commits_and_deduplicates_changed_files(self) -> None:
        with (
            patch.object(agent_remote, "ssh_check", return_value="child\n"),
            patch.object(
                agent_remote,
                "ssh_run",
                return_value=completed_process(stdout="z.txt\na.txt\nz.txt\n"),
            ),
        ):
            result = agent_remote.collect_result(
                "host",
                "/srv/worktree",
                "agent-remote/task",
                "parent",
                0,
                "x" * 20_001,
                "y" * 5_001,
                os_type="posix",
            )

        self.assertEqual(result.new_commit, "child")
        self.assertEqual(result.files_changed, ["a.txt", "z.txt"])
        self.assertEqual(len(result.stdout_tail), 20_000)
        self.assertEqual(len(result.stderr_tail), 5_000)
        self.assertIn("--branch agent-remote/task", result.cleanup_command)

    def test_collects_diff_on_windows_without_and_operator(self) -> None:
        with (
            patch.object(agent_remote, "ssh_check", return_value="child\n"),
            patch.object(
                agent_remote,
                "ssh_run",
                side_effect=[
                    completed_process(stdout="modified.txt\n"),
                    completed_process(stdout="untracked.txt\n"),
                ],
            ) as ssh_run,
        ):
            result = agent_remote.collect_result(
                "host",
                "C:/srv/worktree",
                "agent-remote/task",
                "parent",
                0,
                "out",
                "err",
                os_type="windows",
            )

        self.assertEqual(result.files_changed, ["modified.txt", "untracked.txt"])
        self.assertEqual(ssh_run.call_count, 2)
        cmd1 = ssh_run.call_args_list[0].args[1]
        cmd2 = ssh_run.call_args_list[1].args[1]
        self.assertNotIn("&&", cmd1)
        self.assertNotIn("&&", cmd2)
        self.assertIn("diff --name-only", cmd1)
        self.assertIn("ls-files --others", cmd2)

    def test_reports_no_new_commit_or_output(self) -> None:
        with (
            patch.object(agent_remote, "ssh_check", return_value="parent\n"),
            patch.object(agent_remote, "ssh_run", return_value=completed_process()),
        ):
            result = agent_remote.collect_result(
                "host", "/worktree", "branch", "parent", 1, "", "", os_type="posix"
            )

        self.assertIsNone(result.new_commit)
        self.assertEqual(result.stdout_tail, "")
        self.assertFalse(result.success)


if __name__ == "__main__":
    unittest.main()
