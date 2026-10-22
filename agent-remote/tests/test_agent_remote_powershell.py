"""PowerShell transport tests kept together to share shell and artifact fixtures."""

from __future__ import annotations

import io
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from unittest.mock import patch

from tests.agent_remote_test_support import agent_remote, completed_process

BOOTSTRAP_PREFIX = (
    'powershell -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "'
)
WINDOWS_COMMAND_LINE_LIMIT = 8191
LOCAL_POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


def powershell_path_cases(test):
    """Exercise the same adapters with native and backslash-containing paths."""

    @wraps(test)
    def run_cases(self):
        with self.subTest(executable="native"):
            test(self)
        if LOCAL_POWERSHELL and os.name == "posix":
            with tempfile.TemporaryDirectory(prefix="powershell path ") as directory:
                executable = os.path.join(
                    directory,
                    r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.EXE",
                )
                os.symlink(LOCAL_POWERSHELL, executable)
                with (
                    self.subTest(executable="windows_path_pr35_F4_4_through_F4_8"),
                    patch.dict(globals(), LOCAL_POWERSHELL=executable),
                ):
                    test(self)

    return run_cases


@contextmanager
def cleanup_error_artifacts(error, *, upload_failure=False, timeout=False):
    """Stage real artifacts and deny one filesystem operation during recovery."""
    with tempfile.TemporaryDirectory() as directory:
        remote_home = os.path.join(directory, "home")
        remote_temp = os.path.join(directory, "temp")
        os.makedirs(remote_home)
        os.makedirs(remote_temp)
        environment = {
            **os.environ,
            "TMPDIR": remote_temp,
            "TEMP": remote_temp,
            "TMP": remote_temp,
        }

        def failure():
            if timeout:
                raise subprocess.TimeoutExpired("primary transfer", 5)
            return completed_process(255, stderr="primary transfer failed")

        def upload(host, local_path, remote_name, **kwargs):
            shutil.copy(local_path, os.path.join(remote_home, remote_name))
            with open(
                os.path.join(remote_temp, remote_name.replace(".dat", ".ps1")), "w"
            ) as handle:
                handle.write("exit 1")
            if upload_failure:
                return failure()
            return completed_process()

        def run(host, command, *, input_text=None, **kwargs):
            if input_text is not None:
                match = re.search(r"agent-remote-[0-9a-f]{16}\.ps1", command)
                with open(os.path.join(remote_temp, match.group()), "w") as handle:
                    handle.write("exit 1")
                return failure()
            denied_directory = (
                remote_temp if error.startswith("script_") else remote_home
            )
            mode = 0o500 if error.endswith("_remove") else 0
            # Deny access after PowerShell initializes its working directory.
            command = command.replace(
                '-Command "',
                '-Command "[IO.File]::SetUnixFileMode('
                f"{agent_remote.ps_literal(denied_directory)}, [IO.UnixFileMode]{mode}); ",
                1,
            )
            try:
                return subprocess.run(
                    [shutil.which("bash"), "-c", bash_powershell_command(command)],
                    cwd=remote_home,
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
            finally:
                os.chmod(denied_directory, 0o700)

        yield upload, run, remote_home, remote_temp


def bash_powershell_command(command):
    """Adapt the executable prefix for local Bash integration tests."""
    assert command.startswith("powershell ")
    return shlex.quote(LOCAL_POWERSHELL) + command[len("powershell") :]


def bootstrap_argv(command: str, executable: str) -> list[str]:
    """Turn a bootstrap remote command into an argv list that needs no shell."""
    assert command.startswith(BOOTSTRAP_PREFIX)
    assert command.endswith('"')
    body = command[len(BOOTSTRAP_PREFIX) : -1]
    return [
        executable,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        body,
    ]


class BootstrapCommandTests(unittest.TestCase):
    def test_bootstrap_has_no_dollar_sign_and_only_the_wrapping_double_quotes(
        self,
    ) -> None:
        command = agent_remote.build_powershell_bootstrap("0123456789abcdef")

        self.assertTrue(command.startswith(BOOTSTRAP_PREFIX))
        body = command[len(BOOTSTRAP_PREFIX) : -1]
        self.assertNotIn("$", command)
        self.assertNotIn('"', body)
        self.assertEqual(command.count('"'), 2)
        self.assertNotIn("EncodedCommand", command)
        self.assertNotIn("Invoke-Expression", command)
        self.assertNotIn("FromBase64String", command)
        self.assertLess(len(command), 600)

    def test_bootstrap_rejects_a_token_that_is_not_lowercase_hex(self) -> None:
        for token in ("", "ABCDEF", "abc def", "abc$", 'ab"cd', "ghij"):
            with self.subTest(token=token), self.assertRaises(ValueError):
                agent_remote.build_powershell_bootstrap(token)

    def test_run_uses_a_random_hex_token_for_each_call(self) -> None:
        commands: list[str] = []

        def record(host, command, **keyword_arguments):
            commands.append(command)
            return completed_process()

        with patch.object(agent_remote, "ssh_run", side_effect=record):
            agent_remote.ssh_run_powershell("host", "exit 0")
            agent_remote.ssh_run_powershell("host", "exit 0")

        tokens = [
            re.search(r"agent-remote-([0-9a-f]{16})\.ps1", command).group(1)
            for command in commands
        ]
        self.assertEqual(len(set(tokens)), 2)
        for command in commands:
            self.assertNotIn("$", command)

    def test_run_sends_script_bytes_on_stdin_in_one_ssh_call(self) -> None:
        script = "Write-Output 'h\u00e9llo \u2713'\nexit 0"
        with (
            patch.object(agent_remote, "scp_upload") as scp_upload,
            patch.object(
                agent_remote, "ssh_run", return_value=completed_process(0, "ok")
            ) as ssh_run,
        ):
            result = agent_remote.ssh_run_powershell("host", script, timeout=30)

        self.assertEqual(result.returncode, 0)
        scp_upload.assert_not_called()
        self.assertEqual(ssh_run.call_count, 1)
        self.assertEqual(ssh_run.call_args.kwargs["input_text"], script)
        self.assertEqual(ssh_run.call_args.kwargs["os_type"], "windows")
        self.assertEqual(ssh_run.call_args.kwargs["timeout"], 30)
        self.assertTrue(ssh_run.call_args.args[1].startswith(BOOTSTRAP_PREFIX))

    def test_run_passes_a_twenty_thousand_character_script_through_ssh(self) -> None:
        script = "".join(f"Write-Output 'line {index:05d}'\n" for index in range(700))
        script += "# " + "x" * (20_000 - len(script) - 2)
        self.assertEqual(len(script), 20_000)

        with patch.object(
            agent_remote.subprocess, "run", return_value=completed_process()
        ) as run:
            agent_remote.ssh_run_powershell("host", script)

        argv = run.call_args.args[0]
        self.assertEqual(argv[0], "ssh")
        self.assertLess(len(argv[-1]), WINDOWS_COMMAND_LINE_LIMIT)
        self.assertNotIn("line 00000", argv[-1])
        self.assertEqual(run.call_args.kwargs["input"], script)
        self.assertEqual(len(run.call_args.kwargs["input"]), 20_000)

    @powershell_path_cases
    def test_run_failure_without_payload_makes_no_cleanup_call(self) -> None:
        failure_cases: list[tuple[str, list[object], int, str]] = [
            (
                "exit_code_1_cleanup_ok",
                [completed_process(1, stderr="boom"), completed_process(0)],
                1,
                "boom",
            ),
            (
                "exit_code_1_cleanup_ssh_failed",
                [
                    completed_process(1, stderr="boom"),
                    completed_process(255, stderr="cleanup: connection refused"),
                ],
                1,
                "boom\nwarning: could not remove remote files: cleanup: connection refused",
            ),
            (
                "interrupted_connection_255_cleanup_ok",
                [
                    completed_process(255, stderr="ssh: connection dropped"),
                    completed_process(0),
                ],
                255,
                "ssh: connection dropped",
            ),
            (
                "interrupted_connection_255_cleanup_ssh_failed",
                [
                    completed_process(255, stderr="ssh: connection dropped"),
                    completed_process(255, stderr="cleanup: connection refused"),
                ],
                255,
                "ssh: connection dropped\nwarning: could not remove remote files: cleanup: connection refused",
            ),
        ]
        for name, side_effects, expected_code, expected_stderr in failure_cases:
            with (
                self.subTest(case=name),
                patch.object(
                    agent_remote, "ssh_run", side_effect=side_effects
                ) as ssh_run,
            ):
                result = agent_remote.ssh_run_powershell("host", "exit 1")

            self.assertEqual(result.returncode, expected_code)
            self.assertEqual(result.stderr, expected_stderr)
            self.assertEqual(ssh_run.call_count, 2)
            cleanup_command = ssh_run.call_args_list[1].args[1]
            self.assertIn("Remove-Item -LiteralPath", cleanup_command)
            self.assertIn(".ps1", cleanup_command)
            self.assertNotIn(".dat", cleanup_command)

        bash_executable = shutil.which("bash")
        if LOCAL_POWERSHELL and bash_executable:
            with tempfile.TemporaryDirectory() as temp_dir:
                env = {
                    **os.environ,
                    "TMPDIR": temp_dir,
                    "TEMP": temp_dir,
                    "TMP": temp_dir,
                }
                call_index = 0

                def fake_ssh_run(
                    host: str,
                    command: str,
                    *,
                    input_text: str | None = None,
                    timeout: float | None = None,
                    os_type: str | None = None,
                ) -> subprocess.CompletedProcess:
                    nonlocal call_index
                    call_index += 1
                    if call_index == 1:
                        match = re.search(r"agent-remote-([0-9a-f]{16})\.ps1", command)
                        assert match is not None
                        script_path = os.path.join(
                            temp_dir, f"agent-remote-{match.group(1)}.ps1"
                        )
                        with open(script_path, "w", encoding="utf-8") as handle:
                            handle.write("Start-Sleep -Seconds 10\n")
                        return completed_process(255, stderr="ssh: connection dropped")
                    run_cmd = bash_powershell_command(command)
                    return subprocess.run(
                        [bash_executable, "-c", run_cmd],
                        check=False,
                        cwd=temp_dir,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        env=env,
                    )

                with (
                    self.subTest(case="bash_disconnect_without_payload_removes_script"),
                    patch.object(agent_remote, "ssh_run", side_effect=fake_ssh_run),
                ):
                    result = agent_remote.ssh_run_powershell(
                        "host", "Start-Sleep -Seconds 10"
                    )

                self.assertEqual(result.returncode, 255)
                self.assertIn("ssh: connection dropped", result.stderr)
                self.assertEqual(os.listdir(temp_dir), [])

        if (
            LOCAL_POWERSHELL
            and bash_executable
            and os.name == "posix"
            and os.geteuid() != 0
        ):
            with (
                self.subTest(case="pr35_F4_3_script_lookup_without_payload"),
                cleanup_error_artifacts("script_lookup") as (_, run, home, staging),
                patch.object(agent_remote, "ssh_run", side_effect=run),
            ):
                result = agent_remote.ssh_run_powershell("host", "exit 1")
                self.assertEqual(result.returncode, 255)
                self.assertIn("primary transfer failed", result.stderr)
                self.assertIn("warning: could not remove remote files", result.stderr)
                self.assertIn("denied", result.stderr.lower())
                self.assertEqual(os.listdir(home), [])
                self.assertEqual(len(list(Path(staging).glob("agent-remote-*.ps1"))), 1)

    @powershell_path_cases
    def test_run_timeout_without_payload_propagates(self) -> None:
        timeout_cases: list[tuple[str, list[object], str | None]] = [
            (
                "cleanup_ok",
                [subprocess.TimeoutExpired("ssh", 5), completed_process()],
                None,
            ),
            (
                "cleanup_raises",
                [subprocess.TimeoutExpired("ssh", 5), OSError("ssh drop")],
                "warning: could not remove remote files: ssh drop",
            ),
            (
                "cleanup_ssh_failed",
                [
                    subprocess.TimeoutExpired("ssh", 5),
                    completed_process(255, stderr="cleanup: connection refused"),
                ],
                "warning: could not remove remote files: cleanup: connection refused",
            ),
        ]
        for name, side_effects, expected_warning in timeout_cases:
            with (
                self.subTest(case=name),
                patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
                patch.object(
                    agent_remote, "ssh_run", side_effect=side_effects
                ) as ssh_run,
                self.assertRaises(subprocess.TimeoutExpired),
            ):
                agent_remote.ssh_run_powershell("host", "exit 0", timeout=5)

            self.assertEqual(ssh_run.call_count, 2)
            cleanup_command = ssh_run.call_args_list[1].args[1]
            self.assertIn("Remove-Item -LiteralPath", cleanup_command)
            self.assertIn(".ps1", cleanup_command)
            self.assertNotIn(".dat", cleanup_command)
            if expected_warning:
                self.assertIn(expected_warning, mock_stderr.getvalue())

        bash_executable = shutil.which("bash")
        if LOCAL_POWERSHELL and bash_executable:
            with tempfile.TemporaryDirectory() as temp_dir:
                env = {
                    **os.environ,
                    "TMPDIR": temp_dir,
                    "TEMP": temp_dir,
                    "TMP": temp_dir,
                }
                call_index = 0

                def fake_ssh_run(
                    host: str,
                    command: str,
                    *,
                    input_text: str | None = None,
                    timeout: float | None = None,
                    os_type: str | None = None,
                ) -> subprocess.CompletedProcess:
                    nonlocal call_index
                    call_index += 1
                    if call_index == 1:
                        match = re.search(r"agent-remote-([0-9a-f]{16})\.ps1", command)
                        assert match is not None
                        script_path = os.path.join(
                            temp_dir, f"agent-remote-{match.group(1)}.ps1"
                        )
                        with open(script_path, "w", encoding="utf-8") as handle:
                            handle.write("Start-Sleep -Seconds 10\n")
                        raise subprocess.TimeoutExpired("ssh", timeout or 5)
                    run_cmd = bash_powershell_command(command)
                    return subprocess.run(
                        [bash_executable, "-c", run_cmd],
                        check=False,
                        cwd=temp_dir,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        env=env,
                    )

                with (
                    self.subTest(case="bash_timeout_without_payload_removes_script"),
                    patch.object(agent_remote, "ssh_run", side_effect=fake_ssh_run),
                    self.assertRaises(subprocess.TimeoutExpired),
                ):
                    agent_remote.ssh_run_powershell(
                        "host", "Start-Sleep -Seconds 10", timeout=5
                    )

                self.assertEqual(os.listdir(temp_dir), [])

        if (
            LOCAL_POWERSHELL
            and bash_executable
            and os.name == "posix"
            and os.geteuid() != 0
        ):
            with (
                self.subTest(case="pr35_F4_3_script_lookup_timeout_without_payload"),
                cleanup_error_artifacts("script_lookup", timeout=True) as (
                    _,
                    run,
                    home,
                    staging,
                ),
                patch.object(agent_remote, "ssh_run", side_effect=run),
                patch("sys.stderr", new_callable=io.StringIO) as stderr,
            ):
                with self.assertRaises(subprocess.TimeoutExpired) as raised:
                    agent_remote.ssh_run_powershell("host", "exit 1")
                self.assertEqual(raised.exception.cmd, "primary transfer")
                self.assertIn(
                    "warning: could not remove remote files", stderr.getvalue()
                )
                self.assertIn("denied", stderr.getvalue().lower())
                self.assertEqual(os.listdir(home), [])
                self.assertEqual(len(list(Path(staging).glob("agent-remote-*.ps1"))), 1)

    @unittest.skipUnless(LOCAL_POWERSHELL, "powershell or pwsh is not installed")
    def test_bootstrap_runs_a_multi_line_script_end_to_end(self) -> None:
        script = (
            "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)\n"
            "$greeting = 'h\u00e9llo \u2713 \u65e5\u672c'\n"
            "Write-Output $greeting\n"
            "Write-Output 'second line'\n"
            "exit 7\n"
        )
        cases: list[tuple[str, str, int, str, bool]] = [
            ("valid_script_exit_7", script, 7, "h\u00e9llo \u2713 \u65e5\u672c", True),
            ("parsing_failure", "if (\n", 1, "ParserError", False),
        ]
        command = agent_remote.build_powershell_bootstrap("0123456789abcdef")
        argv = bootstrap_argv(command, LOCAL_POWERSHELL)
        leftover = os.path.join(
            tempfile.gettempdir(), "agent-remote-0123456789abcdef.ps1"
        )

        for name, input_text, expected_code, expected_output, in_stdout in cases:
            with self.subTest(case=name):
                completed = subprocess.run(
                    argv,
                    input=input_text.encode("utf-8"),
                    capture_output=True,
                    check=False,
                    timeout=120,
                )
                output = (
                    completed.stdout.decode("utf-8")
                    if in_stdout
                    else completed.stderr.decode("utf-8")
                )
                self.assertEqual(completed.returncode, expected_code, completed.stderr)
                self.assertIn(expected_output, output)
                self.assertFalse(os.path.exists(leftover))


class PutFileWindowsTests(unittest.TestCase):
    def test_put_file_runs_move_script_with_payload_on_windows(self) -> None:
        with (
            patch.object(agent_remote.sys, "platform", "linux"),
            patch.object(
                agent_remote, "ssh_run_powershell", return_value=completed_process()
            ) as run_powershell,
        ):
            agent_remote.ssh_put_file(
                "host", "C:/srv/it's/file.txt", "contents", os_type="windows"
            )

        script = run_powershell.call_args.args[1]
        self.assertIn("'C:/srv/it''s/file.txt'", script)
        self.assertIn("Test-Path -LiteralPath $path -PathType Container", script)
        self.assertIn("Move-Item -LiteralPath $payloadPath", script)
        self.assertNotIn("FromBase64String", script)
        self.assertEqual(run_powershell.call_args.kwargs["payload"], b"contents")

    @unittest.skipUnless(LOCAL_POWERSHELL, "powershell or pwsh is not installed")
    def test_put_file_moves_payload_into_place_and_rejects_a_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            remote_home = os.path.join(temp_dir, "home")
            os.makedirs(remote_home)
            existing_dir = os.path.join(temp_dir, "dest_dir")
            os.makedirs(existing_dir)
            new_file = os.path.join(temp_dir, "nested", "file.txt")

            def fake_scp(
                host: str,
                local_path: str,
                remote_name: str,
                *,
                timeout: float | None = None,
            ) -> subprocess.CompletedProcess:
                shutil.copy(local_path, os.path.join(remote_home, remote_name))
                return completed_process()

            def fake_ssh_run(
                host: str,
                command: str,
                *,
                input_text: str | None = None,
                timeout: float | None = None,
                os_type: str | None = None,
            ) -> subprocess.CompletedProcess:
                if command.startswith(BOOTSTRAP_PREFIX):
                    argv = bootstrap_argv(command, LOCAL_POWERSHELL)
                else:
                    argv = command.split()
                    argv[0] = LOCAL_POWERSHELL
                return subprocess.run(
                    argv,
                    input=input_text,
                    check=False,
                    cwd=remote_home,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=120,
                )

            with (
                patch.object(agent_remote, "scp_upload", side_effect=fake_scp),
                patch.object(agent_remote, "ssh_run", side_effect=fake_ssh_run),
            ):
                with self.assertRaisesRegex(RuntimeError, "existing directory"):
                    agent_remote.ssh_put_file(
                        "host", existing_dir, "secret payload", os_type="windows"
                    )
                self.assertEqual(os.listdir(existing_dir), [])
                self.assertEqual(os.listdir(remote_home), [])

                agent_remote.ssh_put_file(
                    "host", new_file, "caf\u00e9 contents", os_type="windows"
                )
                with open(new_file, encoding="utf-8") as handle:
                    self.assertEqual(handle.read(), "caf\u00e9 contents")
                self.assertEqual(os.listdir(remote_home), [])

                smart_quote_file = os.path.join(
                    temp_dir, "nested", "it\u2019s_file.txt"
                )
                agent_remote.ssh_put_file(
                    "host", smart_quote_file, "smart quote contents", os_type="windows"
                )
                with open(smart_quote_file, encoding="utf-8") as handle:
                    self.assertEqual(handle.read(), "smart quote contents")
                self.assertEqual(os.listdir(remote_home), [])

    def test_ps_literal_escapes_standard_and_unicode_single_quotes(self) -> None:
        cases = [
            ("ascii_apostrophe", "it's", "'it''s'"),
            (
                "unicode_single_quotes",
                "it's a \u2018test\u2019 \u201afile\u201b",
                "'it''s a \u2018\u2018test\u2019\u2019 \u201a\u201afile\u201b\u201b'",
            ),
        ]
        for name, value, expected in cases:
            with self.subTest(case=name):
                self.assertEqual(agent_remote.ps_literal(value), expected)

    def test_put_file_sends_large_payload_as_file_not_command_line(self) -> None:
        large_content = "A" * 10_000
        with (
            patch.object(agent_remote.sys, "platform", "linux"),
            patch.object(
                agent_remote, "ssh_run_powershell", return_value=completed_process()
            ) as run_powershell,
        ):
            agent_remote.ssh_put_file(
                "host",
                "C:/srv/worktree/.agent-prompt.txt",
                large_content,
                os_type="windows",
            )

        self.assertNotIn("AAAA", run_powershell.call_args.args[1])
        self.assertEqual(
            run_powershell.call_args.kwargs["payload"], large_content.encode("utf-8")
        )


class PayloadUploadTests(unittest.TestCase):
    def test_payload_is_copied_with_scp_then_script_runs_on_stdin(self) -> None:
        uploaded: dict[str, bytes] = {}

        def record_upload(host, local_path, remote_name, *, timeout=None):
            with open(local_path, "rb") as handle:
                uploaded[remote_name] = handle.read()
            return completed_process()

        with (
            patch.object(agent_remote, "scp_upload", side_effect=record_upload),
            patch.object(
                agent_remote, "ssh_run", return_value=completed_process(0, "ok")
            ) as ssh_run,
        ):
            result = agent_remote.ssh_run_powershell(
                "host", "Write-Output 'ok'", payload=b"data"
            )

        self.assertEqual(result.stdout, "ok")
        self.assertEqual(ssh_run.call_count, 1)
        command = ssh_run.call_args.args[1]
        self.assertTrue(command.startswith(BOOTSTRAP_PREFIX))
        script_text = ssh_run.call_args.kwargs["input_text"]
        self.assertIn("Write-Output 'ok'", script_text)
        self.assertIn("Remove-Item -LiteralPath $payloadPath", script_text)
        payload_name = re.search(r"'(agent-remote-[0-9a-f]{16}\.dat)'", script_text)
        self.assertEqual(uploaded[payload_name.group(1)], b"data")
        self.assertEqual(len(uploaded), 1)

    @powershell_path_cases
    def test_payload_is_removed_when_run_fails(self) -> None:
        run_failure_cases: list[tuple[str, list[object]]] = [
            ("exit_code_1", [completed_process(1, stderr="boom"), completed_process()]),
            (
                "cleanup_oserror",
                [completed_process(1, stderr="boom"), OSError("broken pipe")],
            ),
            (
                "cleanup_timeout",
                [
                    completed_process(1, stderr="boom"),
                    subprocess.TimeoutExpired("ssh", 5),
                ],
            ),
            (
                "cleanup_ssh_failed",
                [
                    completed_process(1, stderr="boom"),
                    completed_process(255, stderr="cleanup: connection refused"),
                ],
            ),
        ]
        for name, run_side_effects in run_failure_cases:
            with (
                self.subTest(case=name),
                patch.object(
                    agent_remote, "scp_upload", return_value=completed_process()
                ),
                patch.object(
                    agent_remote, "ssh_run", side_effect=run_side_effects
                ) as ssh_run,
            ):
                result = agent_remote.ssh_run_powershell("host", "exit 1", payload=b"x")

            self.assertEqual(result.returncode, 1)
            self.assertIn("boom", result.stderr)
            if "cleanup" in name:
                self.assertIn("warning: could not remove remote files", result.stderr)
            cleanup_command = ssh_run.call_args_list[1].args[1]
            self.assertIn("Remove-Item -LiteralPath agent-remote-", cleanup_command)
            self.assertIn(".dat", cleanup_command)
            self.assertIn(".ps1", cleanup_command)

        launch_timeout_cases: list[tuple[str, list[object]]] = [
            (
                "launch_timeout_cleanup_ok",
                [subprocess.TimeoutExpired("ssh", 5), completed_process()],
            ),
            (
                "launch_timeout_cleanup_raises",
                [subprocess.TimeoutExpired("ssh", 5), OSError("ssh drop")],
            ),
            (
                "launch_timeout_cleanup_ssh_failed",
                [
                    subprocess.TimeoutExpired("ssh", 5),
                    completed_process(255, stderr="cleanup: connection refused"),
                ],
            ),
        ]
        for name, run_side_effects in launch_timeout_cases:
            with (
                self.subTest(case=name),
                patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
                patch.object(
                    agent_remote, "scp_upload", return_value=completed_process()
                ),
                patch.object(
                    agent_remote, "ssh_run", side_effect=run_side_effects
                ) as ssh_run,
                self.assertRaises(subprocess.TimeoutExpired),
            ):
                agent_remote.ssh_run_powershell("host", "exit 1", payload=b"x")

            self.assertEqual(ssh_run.call_count, 2)
            cleanup_command = ssh_run.call_args_list[1].args[1]
            self.assertIn("Remove-Item -LiteralPath agent-remote-", cleanup_command)
            self.assertIn(".dat", cleanup_command)
            self.assertIn(".ps1", cleanup_command)
            if "raises" in name or "ssh_failed" in name:
                self.assertIn(
                    "warning: could not remove remote files", mock_stderr.getvalue()
                )

        bash_executable = shutil.which("bash")
        if LOCAL_POWERSHELL and bash_executable:
            with tempfile.TemporaryDirectory() as temp_dir:
                remote_home = os.path.join(temp_dir, "home")
                os.makedirs(remote_home)
                normal_temp = os.path.join(temp_dir, "normal_temp")
                os.makedirs(normal_temp)

                def fake_scp_timeout(
                    host: str,
                    local_path: str,
                    remote_name: str,
                    *,
                    timeout: float | None = None,
                ) -> subprocess.CompletedProcess:
                    shutil.copy(local_path, os.path.join(remote_home, remote_name))
                    return completed_process()

                call_index = 0

                def fake_timeout_ssh_run(
                    host: str,
                    command: str,
                    *,
                    input_text: str | None = None,
                    timeout: float | None = None,
                    os_type: str | None = None,
                ) -> subprocess.CompletedProcess:
                    nonlocal call_index
                    call_index += 1
                    if call_index == 1:
                        match = re.search(r"agent-remote-([0-9a-f]{16})\.ps1", command)
                        assert match is not None
                        script_path = os.path.join(
                            normal_temp, f"agent-remote-{match.group(1)}.ps1"
                        )
                        with open(script_path, "w", encoding="utf-8") as handle:
                            handle.write("Start-Sleep -Seconds 10\n")
                        raise subprocess.TimeoutExpired("ssh", timeout or 5)
                    run_cmd = bash_powershell_command(command)
                    env = {
                        **os.environ,
                        "TMPDIR": normal_temp,
                        "TEMP": normal_temp,
                        "TMP": normal_temp,
                    }
                    return subprocess.run(
                        [bash_executable, "-c", run_cmd],
                        check=False,
                        cwd=remote_home,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        env=env,
                    )

                with (
                    self.subTest(case="bash_launch_timeout_removes_both_artifacts"),
                    patch.object(
                        agent_remote, "scp_upload", side_effect=fake_scp_timeout
                    ),
                    patch.object(
                        agent_remote, "ssh_run", side_effect=fake_timeout_ssh_run
                    ),
                    self.assertRaises(subprocess.TimeoutExpired),
                ):
                    agent_remote.ssh_run_powershell(
                        "host", "Start-Sleep -Seconds 10", payload=b"secret", timeout=5
                    )

                self.assertEqual(os.listdir(remote_home), [])
                self.assertEqual(os.listdir(normal_temp), [])

        if LOCAL_POWERSHELL:
            with tempfile.TemporaryDirectory() as temp_dir:
                remote_home = os.path.join(temp_dir, "home")
                os.makedirs(remote_home)
                unavailable_temp = os.path.join(temp_dir, "unavailable_temp")

                def fake_scp(
                    host: str,
                    local_path: str,
                    remote_name: str,
                    *,
                    timeout: float | None = None,
                ) -> subprocess.CompletedProcess:
                    shutil.copy(local_path, os.path.join(remote_home, remote_name))
                    return completed_process()

                def make_fake_ssh_run(temp_path: str):
                    def fake_ssh_run(
                        host: str,
                        command: str,
                        *,
                        input_text: str | None = None,
                        timeout: float | None = None,
                        os_type: str | None = None,
                    ) -> subprocess.CompletedProcess:
                        env = {
                            **os.environ,
                            "TMPDIR": temp_path,
                            "TEMP": temp_path,
                            "TMP": temp_path,
                        }
                        bash = shutil.which("bash")
                        if bash:
                            run_cmd = bash_powershell_command(command)
                            return subprocess.run(
                                [bash, "-c", run_cmd],
                                input=input_text,
                                check=False,
                                cwd=remote_home,
                                capture_output=True,
                                text=True,
                                encoding="utf-8",
                                env=env,
                                timeout=120,
                            )
                        if command.startswith(BOOTSTRAP_PREFIX):
                            argv = bootstrap_argv(command, LOCAL_POWERSHELL)
                        else:
                            argv = command.split()
                            argv[0] = LOCAL_POWERSHELL
                        return subprocess.run(
                            argv,
                            input=input_text,
                            check=False,
                            cwd=remote_home,
                            capture_output=True,
                            text=True,
                            encoding="utf-8",
                            env=env,
                            timeout=120,
                        )

                    return fake_ssh_run

                with (
                    self.subTest(case="unavailable_staging_dir"),
                    patch.object(agent_remote, "scp_upload", side_effect=fake_scp),
                    patch.object(
                        agent_remote,
                        "ssh_run",
                        side_effect=make_fake_ssh_run(unavailable_temp),
                    ),
                ):
                    result = agent_remote.ssh_run_powershell(
                        "host", "Write-Output 'ok'", payload=b"secret"
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(os.listdir(remote_home), [])

                normal_temp = os.path.join(temp_dir, "normal_temp")
                os.makedirs(normal_temp)
                with (
                    self.subTest(case="script_parse_error"),
                    patch.object(agent_remote, "scp_upload", side_effect=fake_scp),
                    patch.object(
                        agent_remote,
                        "ssh_run",
                        side_effect=make_fake_ssh_run(normal_temp),
                    ),
                ):
                    result = agent_remote.ssh_run_powershell(
                        "host", "if (", payload=b"secret"
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("ParserError", result.stderr)
                    self.assertEqual(os.listdir(remote_home), [])
                    self.assertEqual(os.listdir(normal_temp), [])

                call_index = 0

                def fake_disconnect_ssh_run(
                    host: str,
                    command: str,
                    *,
                    input_text: str | None = None,
                    timeout: float | None = None,
                    os_type: str | None = None,
                ) -> subprocess.CompletedProcess:
                    nonlocal call_index
                    call_index += 1
                    if call_index == 1:
                        match = re.search(r"agent-remote-([0-9a-f]{16})\.ps1", command)
                        assert match is not None
                        script_path = os.path.join(
                            normal_temp, f"agent-remote-{match.group(1)}.ps1"
                        )
                        with open(script_path, "w", encoding="utf-8") as handle:
                            handle.write("Start-Sleep -Seconds 10\n")
                        return completed_process(255, stderr="ssh: connection dropped")
                    return make_fake_ssh_run(normal_temp)(
                        host,
                        command,
                        input_text=input_text,
                        timeout=timeout,
                        os_type=os_type,
                    )

                with (
                    self.subTest(case="staged_script_disconnect"),
                    patch.object(agent_remote, "scp_upload", side_effect=fake_scp),
                    patch.object(
                        agent_remote, "ssh_run", side_effect=fake_disconnect_ssh_run
                    ),
                ):
                    result = agent_remote.ssh_run_powershell(
                        "host", "Start-Sleep -Seconds 10", payload=b"secret"
                    )
                    self.assertEqual(result.returncode, 255)
                    self.assertIn("ssh: connection dropped", result.stderr)
                    self.assertEqual(os.listdir(remote_home), [])
                    self.assertEqual(os.listdir(normal_temp), [])

        if (
            LOCAL_POWERSHELL
            and bash_executable
            and os.name == "posix"
            and os.geteuid() != 0
        ):
            for error in (
                "payload_remove",
                "payload_lookup",
                "script_lookup",
                "script_remove",
            ):
                with (
                    self.subTest(case=f"pr35_F4_1_to_F4_3_run_{error}"),
                    cleanup_error_artifacts(error) as (upload, run, home, staging),
                    patch.object(agent_remote, "scp_upload", side_effect=upload),
                    patch.object(agent_remote, "ssh_run", side_effect=run),
                ):
                    result = agent_remote.ssh_run_powershell(
                        "host", "exit 1", payload=b"secret"
                    )
                    self.assertEqual(result.returncode, 255)
                    self.assertIn("primary transfer failed", result.stderr)
                    self.assertIn(
                        "warning: could not remove remote files", result.stderr
                    )
                    self.assertIn("denied", result.stderr.lower())
                    self.assertEqual(
                        len(os.listdir(home)), int(error.startswith("payload_"))
                    )
                    self.assertEqual(
                        len(list(Path(staging).glob("agent-remote-*.ps1"))),
                        int(error.startswith("script_")),
                    )

    @powershell_path_cases
    def test_payload_is_removed_when_upload_fails(self) -> None:
        upload_failure_cases: list[tuple[str, object, object]] = [
            (
                "upload_exit_code_1_cleanup_ok",
                completed_process(1, stderr="no"),
                completed_process(),
            ),
            (
                "upload_exit_code_1_cleanup_oserror",
                completed_process(1, stderr="original upload failure"),
                OSError("conn drop"),
            ),
            (
                "upload_exit_code_1_cleanup_timeout",
                completed_process(1, stderr="original upload failure"),
                subprocess.TimeoutExpired("ssh", 5),
            ),
            (
                "upload_exit_code_1_cleanup_ssh_failed",
                completed_process(1, stderr="original upload failure"),
                completed_process(255, stderr="cleanup: connection refused"),
            ),
        ]
        for name, upload_result, cleanup_effect in upload_failure_cases:
            with (
                self.subTest(case=name),
                patch.object(agent_remote, "scp_upload", return_value=upload_result),
                patch.object(
                    agent_remote, "ssh_run", side_effect=[cleanup_effect]
                ) as ssh_run,
            ):
                result = agent_remote.ssh_run_powershell("host", "exit 0", payload=b"x")

            self.assertEqual(result.returncode, 1)
            self.assertEqual(ssh_run.call_count, 1)
            self.assertIn("Remove-Item", ssh_run.call_args.args[1])
            if (
                isinstance(cleanup_effect, (OSError, subprocess.SubprocessError))
                or getattr(cleanup_effect, "returncode", 0) != 0
            ):
                self.assertIn("original upload failure", result.stderr)
                self.assertIn("warning: could not remove remote files", result.stderr)

        upload_exception_cases: list[tuple[str, object]] = [
            ("upload_timeout", completed_process()),
            ("upload_timeout_cleanup_raises", OSError("net down")),
            (
                "upload_timeout_cleanup_ssh_failed",
                completed_process(255, stderr="cleanup: connection refused"),
            ),
        ]
        for name, cleanup_effect in upload_exception_cases:
            with (
                self.subTest(case=name),
                patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
                patch.object(
                    agent_remote,
                    "scp_upload",
                    side_effect=subprocess.TimeoutExpired("scp", 5),
                ),
                patch.object(
                    agent_remote, "ssh_run", side_effect=[cleanup_effect]
                ) as ssh_run,
                self.assertRaises(subprocess.TimeoutExpired),
            ):
                agent_remote.ssh_run_powershell("host", "exit 0", payload=b"data")

            self.assertEqual(ssh_run.call_count, 1)
            cleanup_command = ssh_run.call_args.args[1]
            self.assertIn("Remove-Item -LiteralPath agent-remote-", cleanup_command)
            self.assertIn(".dat", cleanup_command)
            if (
                isinstance(cleanup_effect, (OSError, subprocess.SubprocessError))
                or getattr(cleanup_effect, "returncode", 0) != 0
            ):
                self.assertIn(
                    "warning: could not remove remote files", mock_stderr.getvalue()
                )

        bash_executable = shutil.which("bash")
        if LOCAL_POWERSHELL and bash_executable:
            with tempfile.TemporaryDirectory() as temp_dir:
                remote_home = os.path.join(temp_dir, "home")
                os.makedirs(remote_home)

                def fake_upload_and_fail(
                    host: str,
                    local_path: str,
                    remote_name: str,
                    *,
                    timeout: float | None = None,
                ) -> subprocess.CompletedProcess:
                    shutil.copy(local_path, os.path.join(remote_home, remote_name))
                    return completed_process(1, stderr="scp transfer failed")

                def fake_upload_and_timeout(
                    host: str,
                    local_path: str,
                    remote_name: str,
                    *,
                    timeout: float | None = None,
                ) -> subprocess.CompletedProcess:
                    shutil.copy(local_path, os.path.join(remote_home, remote_name))
                    raise subprocess.TimeoutExpired("scp", timeout or 5)

                def fake_cleanup_ssh_run(
                    host: str,
                    command: str,
                    *,
                    input_text: str | None = None,
                    timeout: float | None = None,
                    os_type: str | None = None,
                ) -> subprocess.CompletedProcess:
                    run_cmd = bash_powershell_command(command)
                    return subprocess.run(
                        [bash_executable, "-c", run_cmd],
                        check=False,
                        cwd=remote_home,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                    )

                with (
                    self.subTest(case="bash_upload_exit_code_1_removes_dat"),
                    patch.object(
                        agent_remote, "scp_upload", side_effect=fake_upload_and_fail
                    ),
                    patch.object(
                        agent_remote, "ssh_run", side_effect=fake_cleanup_ssh_run
                    ),
                ):
                    result = agent_remote.ssh_run_powershell(
                        "host", "exit 0", payload=b"data"
                    )
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual("scp transfer failed", result.stderr)
                    self.assertEqual(os.listdir(remote_home), [])

                with (
                    self.subTest(case="bash_upload_timeout_removes_dat"),
                    patch.object(
                        agent_remote, "scp_upload", side_effect=fake_upload_and_timeout
                    ),
                    patch.object(
                        agent_remote, "ssh_run", side_effect=fake_cleanup_ssh_run
                    ),
                    patch("sys.stderr", new_callable=io.StringIO) as stderr,
                    self.assertRaises(subprocess.TimeoutExpired),
                ):
                    agent_remote.ssh_run_powershell("host", "exit 0", payload=b"data")
                self.assertEqual(os.listdir(remote_home), [])
                self.assertEqual(stderr.getvalue(), "")

        if (
            LOCAL_POWERSHELL
            and bash_executable
            and os.name == "posix"
            and os.geteuid() != 0
        ):
            for error in ("payload_remove", "payload_lookup", "script_lookup"):
                for timeout in (False, True):
                    with (
                        self.subTest(
                            case=f"pr35_F4_1_to_F4_3_upload_{error}", timeout=timeout
                        ),
                        cleanup_error_artifacts(
                            error, upload_failure=True, timeout=timeout
                        ) as (upload, run, home, staging),
                        patch.object(agent_remote, "scp_upload", side_effect=upload),
                        patch.object(agent_remote, "ssh_run", side_effect=run),
                        patch("sys.stderr", new_callable=io.StringIO) as stderr,
                    ):
                        if timeout:
                            with self.assertRaises(subprocess.TimeoutExpired) as raised:
                                agent_remote.ssh_run_powershell(
                                    "host", "exit 1", payload=b"secret"
                                )
                            self.assertEqual(raised.exception.cmd, "primary transfer")
                            diagnostic = stderr.getvalue()
                        else:
                            result = agent_remote.ssh_run_powershell(
                                "host", "exit 1", payload=b"secret"
                            )
                            self.assertEqual(result.returncode, 255)
                            self.assertIn("primary transfer failed", result.stderr)
                            diagnostic = result.stderr
                        self.assertIn(
                            "warning: could not remove remote files", diagnostic
                        )
                        self.assertIn("denied", diagnostic.lower())
                        self.assertEqual(
                            len(os.listdir(home)), int(error != "script_lookup")
                        )
                        self.assertEqual(
                            len(list(Path(staging).glob("agent-remote-*.ps1"))),
                            int(error == "script_lookup"),
                        )
