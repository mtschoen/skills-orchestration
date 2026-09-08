"""Tests for remote agent invocation and CLI commands."""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch

from tests.agent_remote_test_support import (
    agent_remote,
    completed_process,
    run_arguments,
)


class RemoteAgentInvocationTests(unittest.TestCase):
    def setUp(self) -> None:
        agent_remote.clear_host_os_cache()

    def invoke(
        self,
        agent: str,
        permission_mode: str = "acceptEdits",
        model: str | None = None,
        os_type: str = "posix",
    ) -> str:
        with (
            patch.object(agent_remote, "ssh_put_file") as put_file,
            patch.object(
                agent_remote,
                "ssh_run",
                side_effect=[
                    completed_process(7, "stdout", "stderr"),
                    completed_process(),
                ],
            ) as ssh_run,
        ):
            result = agent_remote.run_remote_agent(
                "host",
                "/srv/worktree",
                "prompt",
                permission_mode,
                30,
                agent,
                model,
                os_type=os_type,
            )

        self.assertEqual(result, (7, "stdout", "stderr"))
        put_file.assert_called_once_with(
            "host", "/srv/worktree/.agent-prompt.txt", "prompt", os_type=os_type
        )
        self.assertEqual(ssh_run.call_args_list[0].kwargs["timeout"], 30)
        if os_type == "posix":
            self.assertIn("rm -f", ssh_run.call_args_list[1].args[1])
        else:
            self.assertIn("os.remove", ssh_run.call_args_list[1].args[1])
        return ssh_run.call_args_list[0].args[1]

    def test_builds_claude_command_on_posix(self) -> None:
        with patch.object(agent_remote.sys, "platform", "linux"):
            command = self.invoke("claude", "plan", os_type="posix")
        self.assertIn("claude -p --permission-mode plan", command)
        self.assertIn("< /srv/worktree/.agent-prompt.txt", command)

    def test_builds_claude_command_on_windows(self) -> None:
        command = self.invoke("claude", "acceptEdits", os_type="windows")
        self.assertNotIn("cd /d", command)
        self.assertNotIn("<", command)
        self.assertIn("python -c", command)
        self.assertIn("base64.b64decode", command)
        b64_part = command.split("base64.b64decode('")[1].split("')")[0]
        decoded = base64.b64decode(b64_part).decode("utf-8")
        self.assertIn("shutil.which", decoded)
        self.assertIn("claude", decoded)
        self.assertIn("cwd='/srv/worktree'", decoded)

    def test_builds_agy_commands_for_plan_and_edit_modes(self) -> None:
        plan_command = self.invoke("agy", "plan", "provider/model", os_type="posix")
        edit_command = self.invoke("agy", "acceptEdits", os_type="posix")
        self.assertIn('"--mode", "plan"', plan_command)
        self.assertIn('"--model", "provider/model"', plan_command)
        self.assertIn('"--dangerously-skip-permissions"', edit_command)
        self.assertIn('"accept-edits"', edit_command)

    def test_builds_opencode_pi_and_codex_commands(self) -> None:
        opencode_command = self.invoke(
            "opencode", model="provider/model", os_type="posix"
        )
        pi_command = self.invoke("pi", model="provider/model", os_type="posix")
        codex_command = self.invoke("codex", os_type="posix")
        self.assertIn('"opencode", "run", "--auto"', opencode_command)
        self.assertIn('"pi", "-p"', pi_command)
        self.assertIn('"codex", "exec", "--dangerously-bypass', codex_command)

    def test_builds_windows_python_commands_with_base64_bootstrap(self) -> None:
        windows_command = self.invoke("agy", "acceptEdits", os_type="windows")
        self.assertNotIn("cd /d", windows_command)
        self.assertIn("python -c", windows_command)
        self.assertIn("base64.b64decode", windows_command)
        b64_part = windows_command.split("base64.b64decode('")[1].split("')")[0]
        decoded = base64.b64decode(b64_part).decode("utf-8")
        self.assertIn("shutil.which", decoded)
        self.assertIn("cwd='/srv/worktree'", decoded)
        self.assertIn("is_batch", decoded)

    def test_rejects_unknown_agent(self) -> None:
        with (
            patch.object(agent_remote, "ssh_put_file"),
            self.assertRaisesRegex(ValueError, "Unknown agent"),
        ):
            agent_remote.run_remote_agent(
                "host",
                "/worktree",
                "prompt",
                "default",
                1,
                "unknown",
                os_type="posix",
            )

    def test_cleanup_failure_appends_warning_without_raising(self) -> None:
        with (
            patch.object(agent_remote, "ssh_put_file"),
            patch.object(
                agent_remote,
                "ssh_run",
                side_effect=[
                    completed_process(0, "stdout", "stderr"),
                    subprocess.TimeoutExpired("ssh", 5),
                ],
            ),
        ):
            exit_code, stdout, stderr = agent_remote.run_remote_agent(
                "host",
                "/srv/worktree",
                "prompt",
                "acceptEdits",
                30,
                "claude",
                os_type="posix",
            )

        self.assertEqual((exit_code, stdout), (0, "stdout"))
        self.assertIn("stderr", stderr)
        self.assertIn("warning: could not remove remote prompt file", stderr)

    def test_windows_launcher_resolves_npm_cmd_and_preserves_multiline_prompt(
        self,
    ) -> None:
        command = self.invoke(
            "opencode",
            permission_mode="acceptEdits",
            os_type="windows",
        )
        self.assertNotIn("cd /d", command)
        b64_part = command.split("base64.b64decode('")[1].split("')")[0]
        decoded = base64.b64decode(b64_part).decode("utf-8")
        self.assertIn("shutil.which(args[0])", decoded)
        self.assertIn(
            "is_batch = str(prog).lower().endswith(('.cmd', '.bat'))", decoded
        )
        self.assertIn("cmd.exe /d /c", decoded)
        self.assertIn(
            "subprocess.run(command, stdin=f, cwd='/srv/worktree', shell=False)",
            decoded,
        )
        self.assertIn("subprocess.run(args, cwd='/srv/worktree', shell=False)", decoded)
        self.assertNotIn("shell=True", decoded)

    def test_windows_launcher_keeps_large_prompt_out_of_cmd_exe_argv(self) -> None:
        large_prompt = "P" * 10_000
        with (
            patch.object(agent_remote, "ssh_put_file"),
            patch.object(
                agent_remote,
                "ssh_run",
                side_effect=[
                    completed_process(0, "ok", ""),
                    completed_process(),
                ],
            ) as ssh_run,
        ):
            agent_remote.run_remote_agent(
                "host",
                "/srv/worktree",
                large_prompt,
                "acceptEdits",
                30,
                "opencode",
                os_type="windows",
            )

        cmd = ssh_run.call_args_list[0].args[1]
        b64_part = cmd.split("base64.b64decode('")[1].split("')")[0]
        decoded = base64.b64decode(b64_part).decode("utf-8")

        # Simulate execution of the decoded bootstrap with mock resolver and subprocess
        mock_run = completed_process(0)
        with (
            patch("shutil.which", return_value=r"C:\npm\opencode.cmd"),
            patch("subprocess.run", return_value=mock_run) as run_mock,
            patch("pathlib.Path.read_text", return_value=large_prompt),
            patch("builtins.open", unittest.mock.mock_open(read_data=large_prompt)),
        ):
            # Run the decoded bootstrap in an isolated namespace
            namespace: dict = {}
            with self.assertRaises(SystemExit) as cm:
                exec(decoded, namespace)

        self.assertEqual(cm.exception.code, 0)

        self.assertTrue(run_mock.called)
        called_args, called_kwargs = run_mock.call_args
        # shell=False without shell parser, batch launcher invoked via cmd.exe /d /c
        self.assertFalse(called_kwargs.get("shell", False))
        command_line = called_args[0]
        self.assertIsInstance(command_line, str)
        self.assertTrue(command_line.startswith('cmd.exe /d /c "'))
        self.assertTrue(command_line.endswith('"'))
        self.assertIn(r'"C:\npm\opencode.cmd"', command_line)
        # Stdin file stream was provided
        self.assertIsNotNone(called_kwargs.get("stdin"))
        # Prompt data was kept out of command line to satisfy cmd.exe 8191 limit
        self.assertNotIn("PPPP", command_line)
        self.assertLess(len(command_line), 500)

    def test_windows_launcher_claude_batch_launcher_without_shell_true(self) -> None:
        command = self.invoke(
            "claude",
            permission_mode="acceptEdits",
            os_type="windows",
        )
        b64_part = command.split("base64.b64decode('")[1].split("')")[0]
        decoded = base64.b64decode(b64_part).decode("utf-8")
        self.assertIn("shutil.which(args[0])", decoded)
        self.assertIn(
            "is_batch = str(prog).lower().endswith(('.cmd', '.bat'))", decoded
        )
        self.assertIn("cmd.exe /d /c", decoded)
        self.assertNotIn("shell=True", decoded)
        self.assertNotIn("shell=is_batch", decoded)
        self.assertIn(
            "subprocess.run(command, stdin=f, cwd='/srv/worktree', shell=False)",
            decoded,
        )

        mock_run = completed_process(0)
        with (
            patch("shutil.which", return_value=r"C:\npm\claude.cmd"),
            patch("subprocess.run", return_value=mock_run) as run_mock,
            patch("builtins.open", unittest.mock.mock_open(read_data="hello")),
        ):
            namespace: dict = {}
            with self.assertRaises(SystemExit) as cm:
                exec(decoded, namespace)

        self.assertEqual(cm.exception.code, 0)
        self.assertTrue(run_mock.called)
        called_args, called_kwargs = run_mock.call_args
        self.assertFalse(called_kwargs.get("shell", False))
        command_line = called_args[0]
        self.assertIsInstance(command_line, str)
        self.assertTrue(command_line.startswith('cmd.exe /d /c "'))
        self.assertTrue(command_line.endswith('"'))
        self.assertIn(r'"C:\npm\claude.cmd"', command_line)
        self.assertEqual(
            command_line,
            r'cmd.exe /d /c ""C:\npm\claude.cmd" -p --permission-mode acceptEdits"',
        )
        self.assertIsNotNone(called_kwargs.get("stdin"))

    def test_windows_launcher_preserves_outer_quotes_for_batch_path_with_spaces_and_parentheses(
        self,
    ) -> None:
        command = self.invoke(
            "pi",
            permission_mode="acceptEdits",
            os_type="windows",
        )
        b64_part = command.split("base64.b64decode('")[1].split("')")[0]
        decoded = base64.b64decode(b64_part).decode("utf-8")

        mock_run = completed_process(0)
        batch_path = r"C:\Program Files (x86)\nodejs\pi.cmd"
        with (
            patch("shutil.which", return_value=batch_path),
            patch("subprocess.run", return_value=mock_run) as run_mock,
            patch("builtins.open", unittest.mock.mock_open(read_data="hello")),
        ):
            namespace: dict = {}
            with self.assertRaises(SystemExit) as cm:
                exec(decoded, namespace)

        self.assertEqual(cm.exception.code, 0)
        self.assertTrue(run_mock.called)
        called_args, called_kwargs = run_mock.call_args
        self.assertFalse(called_kwargs.get("shell", False))
        command_line = called_args[0]
        self.assertIsInstance(command_line, str)
        # Outer command quotes wrap the entire command passed to cmd.exe /d /c
        self.assertTrue(command_line.startswith('cmd.exe /d /c "'))
        self.assertTrue(command_line.endswith('"'))
        # Inner path containing spaces and parentheses is explicitly quoted
        self.assertIn(r'"C:\Program Files (x86)\nodejs\pi.cmd"', command_line)
        self.assertEqual(
            command_line,
            r'cmd.exe /d /c ""C:\Program Files (x86)\nodejs\pi.cmd" -p"',
        )


class RunCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        agent_remote.clear_host_os_cache()

    def test_refuses_unapproved_bypass_mode(self) -> None:
        arguments = run_arguments(permission_mode="bypassPermissions")
        error = StringIO()
        with (
            patch.dict(agent_remote.os.environ, {}, clear=True),
            redirect_stderr(error),
        ):
            exit_code = agent_remote.cmd_run(arguments)

        self.assertEqual(exit_code, 2)
        self.assertIn("REMOTE_AGENT_ALLOW_BYPASS=1", error.getvalue())

    def test_runs_claude_and_seeds_extended_allowlist(self) -> None:
        arguments = run_arguments(
            agent="claude", extra_allow=["Bash(sudo *)"], os="posix"
        )
        result = agent_remote.RunResult(
            "host", "branch", "/worktree", "parent", "child", [], 0, "", "", "cleanup"
        )
        output = StringIO()
        with (
            patch.object(
                agent_remote, "unmangle_msys_path", return_value="/repo"
            ) as unmangle,
            patch.object(
                agent_remote, "ensure_worktree", return_value=("/worktree", "parent")
            ),
            patch.object(agent_remote, "seed_settings") as seed_settings,
            patch.object(
                agent_remote, "run_remote_agent", return_value=(0, "out", "err")
            ),
            patch.object(agent_remote, "collect_result", return_value=result),
            patch.dict(
                agent_remote.os.environ, {"REMOTE_AGENT_TIMEOUT": "12"}, clear=True
            ),
            redirect_stdout(output),
        ):
            exit_code = agent_remote.cmd_run(arguments)

        self.assertEqual(exit_code, 0)
        unmangle.assert_called_once_with("/srv/project")
        self.assertEqual(arguments.repo_path, "/repo")
        self.assertEqual(
            seed_settings.call_args.args[2],
            [*agent_remote.DEFAULT_REMOTE_ALLOWLIST, "Bash(sudo *)"],
        )
        self.assertTrue(json.loads(output.getvalue())["success"])

    def test_returns_one_when_agent_result_is_unsuccessful(self) -> None:
        arguments = run_arguments(os="posix")
        result = agent_remote.RunResult(
            "host", "branch", "/worktree", "parent", None, [], 5, "", "", "cleanup"
        )
        with (
            patch.object(
                agent_remote, "ensure_worktree", return_value=("/worktree", "parent")
            ),
            patch.object(agent_remote, "run_remote_agent", return_value=(5, "", "")),
            patch.object(agent_remote, "collect_result", return_value=result),
            patch.dict(agent_remote.os.environ, {}, clear=True),
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(agent_remote.cmd_run(arguments), 1)

    def test_autodetects_each_supported_agent_environment(self) -> None:
        scenarios = [
            ({"ANTIGRAVITY_AGENT": "1"}, "agy"),
            ({"OPENCODE_TEST": "1"}, "opencode"),
            ({"CLAUDE_CODE_TEST": "1"}, "claude"),
            ({"CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1"}, "claude"),
            ({"PI_TEST": "1"}, "pi"),
            ({"CODEX_TEST": "1"}, "codex"),
            ({}, "opencode"),
        ]
        for environment, expected_agent in scenarios:
            with self.subTest(expected_agent=expected_agent, environment=environment):
                arguments = run_arguments(agent=None, os="posix")
                result = agent_remote.RunResult(
                    "host",
                    "branch",
                    "/worktree",
                    "parent",
                    None,
                    [],
                    0,
                    "",
                    "",
                    "cleanup",
                )
                with (
                    patch.object(
                        agent_remote,
                        "ensure_worktree",
                        return_value=("/worktree", "parent"),
                    ),
                    patch.object(agent_remote, "seed_settings"),
                    patch.object(
                        agent_remote, "run_remote_agent", return_value=(0, "", "")
                    ) as run_agent,
                    patch.object(agent_remote, "collect_result", return_value=result),
                    patch.dict(agent_remote.os.environ, environment, clear=True),
                    redirect_stdout(StringIO()),
                ):
                    exit_code = agent_remote.cmd_run(arguments)

                self.assertEqual(exit_code, 0)
                self.assertEqual(run_agent.call_args.kwargs["agent"], expected_agent)

    def test_generates_branch_and_uses_configured_timeout(self) -> None:
        arguments = run_arguments(branch=None, os="posix")
        result = agent_remote.RunResult(
            "host", "branch", "/worktree", "parent", None, [], 0, "", "", "cleanup"
        )
        with (
            patch.object(agent_remote.time, "time", return_value=1234),
            patch.object(
                agent_remote, "ensure_worktree", return_value=("/worktree", "parent")
            ) as ensure,
            patch.object(
                agent_remote, "run_remote_agent", return_value=(0, "", "")
            ) as run_agent,
            patch.object(agent_remote, "collect_result", return_value=result),
            patch.dict(
                agent_remote.os.environ, {"REMOTE_AGENT_TIMEOUT": "42"}, clear=True
            ),
            redirect_stdout(StringIO()),
        ):
            agent_remote.cmd_run(arguments)

        self.assertEqual(ensure.call_args.args[2], "agent-remote/auto-1234")
        self.assertEqual(run_agent.call_args.kwargs["timeout"], 42)

    def test_reports_timeout_and_other_exceptions_as_json(self) -> None:
        scenarios = [
            (subprocess.TimeoutExpired("ssh", 5), 3, "timeout"),
            (RuntimeError("broken"), 4, "RuntimeError"),
        ]
        for exception, expected_code, expected_error in scenarios:
            with self.subTest(expected_code=expected_code):
                output = StringIO()
                with (
                    patch.object(
                        agent_remote, "ensure_worktree", side_effect=exception
                    ),
                    patch.dict(agent_remote.os.environ, {}, clear=True),
                    redirect_stdout(output),
                ):
                    exit_code = agent_remote.cmd_run(run_arguments(os="posix"))
                payload = json.loads(output.getvalue())
                self.assertEqual(exit_code, expected_code)
                self.assertIn(
                    expected_error, payload.get("error_type", payload["error"])
                )

    def test_reports_detection_failure_as_json(self) -> None:
        scenarios = [
            (subprocess.TimeoutExpired("ssh", 15), 3, "timeout"),
            (RuntimeError("OS detection failed"), 4, "RuntimeError"),
        ]
        for exception, expected_code, expected_error in scenarios:
            with self.subTest(expected_code=expected_code):
                output = StringIO()
                with (
                    patch.object(
                        agent_remote, "detect_remote_os", side_effect=exception
                    ),
                    patch.dict(agent_remote.os.environ, {}, clear=True),
                    redirect_stdout(output),
                ):
                    exit_code = agent_remote.cmd_run(run_arguments(os="auto"))
                payload = json.loads(output.getvalue())
                self.assertEqual(exit_code, expected_code)
                self.assertIn(
                    expected_error, payload.get("error_type", payload["error"])
                )


class CleanupCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        agent_remote.clear_host_os_cache()

    def test_requires_repo_path(self) -> None:
        arguments = argparse.Namespace(
            host="host", branch="branch", repo_path="", os="posix"
        )
        with redirect_stderr(StringIO()) as error:
            exit_code = agent_remote.cmd_cleanup(arguments)
        self.assertEqual(exit_code, 2)
        self.assertIn("--repo-path is required", error.getvalue())

    def test_removes_worktree_and_branch_on_posix(self) -> None:
        arguments = argparse.Namespace(
            host="host",
            branch="agent-remote/task",
            repo_path="/srv/project",
            os="posix",
        )
        output = StringIO()
        with (
            patch.object(agent_remote, "ssh_check") as ssh_check,
            redirect_stdout(output),
        ):
            exit_code = agent_remote.cmd_cleanup(arguments)

        self.assertEqual(exit_code, 0)
        self.assertIn("worktree remove --force", ssh_check.call_args.args[1])
        self.assertEqual(json.loads(output.getvalue())["branch"], "agent-remote/task")

    def test_removes_worktree_and_branch_on_windows(self) -> None:
        arguments = argparse.Namespace(
            host="host",
            branch="agent-remote/task",
            repo_path="C:/srv/project",
            os="windows",
        )
        output = StringIO()
        with (
            patch.object(agent_remote, "ssh_check") as ssh_check,
            redirect_stdout(output),
        ):
            exit_code = agent_remote.cmd_cleanup(arguments)

        self.assertEqual(exit_code, 0)
        self.assertEqual(ssh_check.call_count, 2)
        wt_cmd = ssh_check.call_args_list[0].args[1]
        br_cmd = ssh_check.call_args_list[1].args[1]
        self.assertIn('worktree remove --force "C:/srv/agent-remote-worktrees', wt_cmd)
        self.assertIn('branch -D "agent-remote/task"', br_cmd)

    def test_reports_cleanup_failure(self) -> None:
        arguments = argparse.Namespace(
            host="host", branch="branch", repo_path="/repo", os="posix"
        )
        output = StringIO()
        with (
            patch.object(
                agent_remote, "ssh_check", side_effect=RuntimeError("failure")
            ),
            redirect_stdout(output),
        ):
            exit_code = agent_remote.cmd_cleanup(arguments)
        self.assertEqual(exit_code, 1)
        self.assertEqual(json.loads(output.getvalue())["error_type"], "RuntimeError")

    def test_reports_detection_failure_as_json(self) -> None:
        arguments = argparse.Namespace(
            host="host", branch="branch", repo_path="/repo", os="auto"
        )
        output = StringIO()
        with (
            patch.object(
                agent_remote,
                "detect_remote_os",
                side_effect=RuntimeError("detection failed"),
            ),
            redirect_stdout(output),
        ):
            exit_code = agent_remote.cmd_cleanup(arguments)
        self.assertEqual(exit_code, 1)
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["success"])
        self.assertEqual(payload["error_type"], "RuntimeError")


class ProbeCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        agent_remote.clear_host_os_cache()

    def probe_arguments(self, **overrides: object) -> argparse.Namespace:
        values: dict[str, object] = {
            "host": "host",
            "repo_path": "/srv/project",
            "os": "posix",
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_emits_validated_posix_probe_result(self) -> None:
        probe_json = json.dumps(
            {
                "os": "posix",
                "shell": "/bin/bash",
                "user": "worker",
                "hostname": "llamabox",
                "uname": "Linux 6.18",
                "repo_path_exists": True,
                "git": "git version 2.44.0",
                "claude": "2.1.220",
                "agy": "0.1.0",
                "opencode": "missing",
                "pi": "missing",
                "codex": "missing",
                "python": "Python 3.13.2",
                "path": "/usr/bin",
            }
        )
        output = StringIO()
        with (
            patch.object(
                agent_remote,
                "ssh_run",
                return_value=completed_process(stdout=probe_json),
            ) as ssh_run,
            redirect_stdout(output),
        ):
            exit_code = agent_remote.cmd_probe(self.probe_arguments())

        self.assertEqual(exit_code, 0)
        self.assertIn("repo_path_exists", ssh_run.call_args.args[1])
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["os"], "posix")
        self.assertEqual(payload["shell"], "/bin/bash")
        self.assertEqual(payload["user"], "worker")
        self.assertEqual(payload["success"], True)
        self.assertEqual(payload["host"], "host")

    def test_emits_validated_windows_probe_result(self) -> None:
        probe_json = json.dumps(
            {
                "os": "windows",
                "shell": "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
                "configured_shell": "C:\\WINDOWS\\system32\\cmd.exe",
                "user": "mtsch",
                "hostname": "chonkers",
                "uname": "Microsoft Windows [Version 10.0.26200.8894]",
                "repo_path_exists": True,
                "git": "git version 2.44.0.windows.1",
                "claude": "2.1.220",
                "agy": "0.1.0",
                "opencode": "missing",
                "pi": "missing",
                "codex": "missing",
                "python": "Python 3.13.14",
                "path": "C:\\Windows;C:\\Users\\mtsch\\AppData\\Local\\agy\\bin",
            }
        )
        output = StringIO()
        with (
            patch.object(
                agent_remote,
                "ssh_run",
                return_value=completed_process(stdout=probe_json),
            ) as ssh_run,
            redirect_stdout(output),
        ):
            exit_code = agent_remote.cmd_probe(
                self.probe_arguments(
                    host="mtsch@chonkers",
                    repo_path="C:/Users/mtsch/schoen-lab",
                    os="windows",
                )
            )

        self.assertEqual(exit_code, 0)
        raw_cmd = ssh_run.call_args.args[1]
        self.assertIn("powershell -NoProfile -EncodedCommand", raw_cmd)
        b64_part = raw_cmd.split()[-1]
        decoded_script = base64.b64decode(b64_part).decode("utf-16-le")
        self.assertIn("C:/Users/mtsch/schoen-lab", decoded_script)
        self.assertIn("cmd /c ver", decoded_script)
        self.assertIn("(Get-Process -Id $PID).Path", decoded_script)
        self.assertIn("configured_shell = $confShell", decoded_script)

        payload = json.loads(output.getvalue())
        self.assertEqual(payload["os"], "windows")
        self.assertEqual(
            payload["shell"],
            "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
        )
        self.assertEqual(payload["configured_shell"], "C:\\WINDOWS\\system32\\cmd.exe")
        self.assertEqual(payload["user"], "mtsch")
        self.assertEqual(
            payload["uname"], "Microsoft Windows [Version 10.0.26200.8894]"
        )
        self.assertEqual(payload["success"], True)
        self.assertEqual(payload["host"], "mtsch@chonkers")

    def test_probe_command_distinguishes_executing_shell_from_configured_shell(
        self,
    ) -> None:
        posix_probe = agent_remote.build_probe_command("/repo", "posix")
        self.assertIn('"${BASH:-$0}"', posix_probe)
        self.assertIn('"${SHELL:-}"', posix_probe)

        windows_probe = agent_remote.build_probe_command("C:/repo", "windows")
        b64_part = windows_probe.split()[-1]
        decoded = base64.b64decode(b64_part).decode("utf-16-le")
        self.assertIn("(Get-Process -Id $PID).Path", decoded)
        self.assertIn("configured_shell = $confShell", decoded)
        self.assertIn("$env:COMSPEC", decoded)

    def test_reports_remote_probe_failure(self) -> None:
        output = StringIO()
        with (
            patch.object(
                agent_remote,
                "ssh_run",
                return_value=completed_process(1, stderr="offline"),
            ),
            redirect_stdout(output),
        ):
            exit_code = agent_remote.cmd_probe(self.probe_arguments())
        self.assertEqual(exit_code, 1)
        self.assertEqual(json.loads(output.getvalue())["stderr"], "offline")

    def test_reports_invalid_probe_json(self) -> None:
        output = StringIO()
        with (
            patch.object(
                agent_remote,
                "ssh_run",
                return_value=completed_process(stdout="not json"),
            ),
            redirect_stdout(output),
        ):
            exit_code = agent_remote.cmd_probe(self.probe_arguments())
        self.assertEqual(exit_code, 1)
        self.assertEqual(json.loads(output.getvalue())["error_type"], "JSONDecodeError")

    def test_reports_detection_failure_as_json(self) -> None:
        output = StringIO()
        with (
            patch.object(
                agent_remote,
                "detect_remote_os",
                side_effect=RuntimeError("detection failed"),
            ),
            redirect_stdout(output),
        ):
            exit_code = agent_remote.cmd_probe(self.probe_arguments(os="auto"))
        self.assertEqual(exit_code, 1)
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["success"])
        self.assertEqual(payload["error_type"], "RuntimeError")


class ParserTests(unittest.TestCase):
    def test_parser_accepts_all_subcommands(self) -> None:
        parser = agent_remote.build_parser()
        run = parser.parse_args(
            [
                "run",
                "--host",
                "host",
                "--repo-path",
                "/repo",
                "--prompt",
                "prompt",
                "--agent",
                "codex",
                "--model",
                "model",
                "--extra-allow",
                "Read(/tmp/*)",
                "--os",
                "windows",
            ]
        )
        cleanup = parser.parse_args(
            [
                "cleanup",
                "--host",
                "host",
                "--branch",
                "branch",
                "--repo-path",
                "/repo",
                "--os",
                "posix",
            ]
        )
        probe = parser.parse_args(
            ["probe", "--host", "host", "--repo-path", "/repo", "--os", "auto"]
        )

        self.assertIs(run.func, agent_remote.cmd_run)
        self.assertEqual(run.agent, "codex")
        self.assertEqual(run.extra_allow, ["Read(/tmp/*)"])
        self.assertEqual(run.os, "windows")
        self.assertIs(cleanup.func, agent_remote.cmd_cleanup)
        self.assertEqual(cleanup.os, "posix")
        self.assertIs(probe.func, agent_remote.cmd_probe)
        self.assertEqual(probe.os, "auto")

    def test_main_dispatches_to_selected_command(self) -> None:
        with patch.object(agent_remote, "build_parser") as build_parser:
            build_parser.return_value.parse_args.return_value = argparse.Namespace(
                func=lambda arguments: 23
            )
            self.assertEqual(agent_remote.main(["probe"]), 23)


if __name__ == "__main__":
    unittest.main()
