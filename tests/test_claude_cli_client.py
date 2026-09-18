from __future__ import annotations

from contextlib import redirect_stderr
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "skills" / "claude-cli-client" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import claude_cli_client as client  # noqa: E402


def parsed_args(*argv: str):
    with mock.patch.object(sys, "argv", ["claude_cli_client.py", *argv]):
        return client.parse_args()


class FakeProcess:
    def __init__(self, stdout: str, stderr: str = "", returncode: int = 0, *, alive_after_stdout: bool = False):
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)
        self.stdin = None
        self.returncode = returncode
        self.pid = 1234
        self.killed = False
        self.wait_calls = 0
        self.alive_after_stdout = alive_after_stdout

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        if self.alive_after_stdout and not self.killed:
            raise subprocess.TimeoutExpired("fake-claude", timeout or 0)
        return self.returncode


class ClaudeCliClientTests(unittest.TestCase):
    def test_tools_preserves_omitted_vs_explicit_empty(self) -> None:
        for args, expected in (
            (parsed_args("prompt"), None),
            (parsed_args("--tools", "", "prompt"), ""),
            (parsed_args("--tools", "default", "prompt"), "default"),
            (parsed_args("--tools", "Bash,Read", "prompt"), "Bash,Read"),
        ):
            command = client.build_claude_command(args, ".", "prompt", "session-1", False)
            if expected is None:
                self.assertNotIn("--tools", command)
            else:
                index = command.index("--tools")
                self.assertEqual(command[index + 1], expected)

    def test_native_windows_executable_is_preferred(self) -> None:
        with mock.patch.object(client.sys, "platform", "win32"), mock.patch.object(
            client.shutil, "which", side_effect=lambda value: "C:/bin/claude.exe" if value == "claude.exe" else None
        ):
            self.assertEqual(client.resolve_claude_bin("claude"), "C:/bin/claude.exe")

    def test_completed_assistant_text_replaces_partial_stream(self) -> None:
        args = parsed_args("--json", "--tools", "", "prompt")
        process = FakeProcess(
            "\n".join(
                [
                    json.dumps({"type": "stream_event", "event": {"type": "message_start", "message": {"id": "turn-1"}}}),
                    json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "partial"}}}),
                    json.dumps({"type": "assistant", "message": {"id": "turn-1", "content": [{"type": "text", "text": "complete"}]}}),
                    json.dumps({"type": "result", "subtype": "success", "result": "complete"}),
                ]
            )
            + "\n"
        )
        with mock.patch.object(client.subprocess, "Popen", return_value=process):
            code, result, session = client.run_turn(args, ".", "prompt", "session-1", False, 1, 0)
        self.assertEqual(code, client.EXIT_SUCCESS)
        self.assertEqual(session, "session-1")
        self.assertEqual(result["text"], "complete")
        self.assertFalse(process.killed)

    def test_stderr_is_drained_without_blocking_stdout(self) -> None:
        args = parsed_args("--json", "prompt")
        process = FakeProcess(
            json.dumps({"type": "result", "subtype": "success", "result": "done"}) + "\n",
            stderr="noise\n" * 10000,
        )
        with mock.patch.object(client.subprocess, "Popen", return_value=process):
            code, result, _ = client.run_turn(args, ".", "prompt", "session-1", False, 1, 0)
        self.assertEqual(code, client.EXIT_SUCCESS)
        self.assertEqual(result["text"], "done")
        self.assertFalse(process.killed)

    def test_real_child_stderr_flood_does_not_block_stdout(self) -> None:
        args = parsed_args("--json", "--no-stream", "prompt")
        script = (
            "import json, sys; "
            "sys.stderr.write('x' * 200000); sys.stderr.flush(); "
            "sys.stdout.write(json.dumps({'type': 'result', 'subtype': 'success', 'result': 'done'}) + '\\n'); "
            "sys.stdout.flush()"
        )
        with mock.patch.object(client, "build_claude_command", return_value=[sys.executable, "-c", script]):
            with redirect_stderr(io.StringIO()):
                code, result, _ = client.run_turn(args, ".", "prompt", "session-1", False, 3, 0)
        self.assertEqual(code, client.EXIT_SUCCESS)
        self.assertEqual(result["text"], "done")

    def test_cleanup_is_bounded_when_descendant_holds_inherited_pipes(self) -> None:
        args = parsed_args("--json", "--no-stream", "prompt")
        with tempfile.TemporaryDirectory() as directory:
            pid_path = Path(directory) / "descendant.pid"
            script = "\n".join(
                [
                    "import subprocess, sys, time",
                    "pid_path = sys.argv[1]",
                    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])",
                    "with open(pid_path, 'w', encoding='ascii') as handle:",
                    "    handle.write(str(child.pid))",
                    "    handle.flush()",
                    "time.sleep(30)",
                ]
            )
            started = time.perf_counter()
            turn_elapsed = None
            try:
                with mock.patch.object(
                    client,
                    "build_claude_command",
                    return_value=[sys.executable, "-c", script, str(pid_path)],
                ), redirect_stderr(io.StringIO()):
                    code, result, _ = client.run_turn(args, ".", "prompt", "session-1", False, 0.1, 0)
                turn_elapsed = time.perf_counter() - started
            finally:
                if pid_path.exists():
                    descendant_pid = int(pid_path.read_text(encoding="ascii"))
                    if sys.platform.startswith("win"):
                        subprocess.run(
                            ["taskkill", "/PID", str(descendant_pid), "/F"],
                            check=False,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                    else:
                        try:
                            os.kill(descendant_pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
        self.assertEqual(code, client.EXIT_TIMEOUT)
        self.assertIsNone(result)
        self.assertIsNotNone(turn_elapsed)
        self.assertLess(turn_elapsed, client.PROCESS_CLEANUP_TIMEOUT + 0.8)

    def test_child_that_stays_alive_after_stdout_closes_is_reaped_on_timeout(self) -> None:
        args = parsed_args("--json", "prompt")
        process = FakeProcess("", alive_after_stdout=True)
        with mock.patch.object(client.subprocess, "Popen", return_value=process):
            code, result, _ = client.run_turn(args, ".", "prompt", "session-1", False, 0.01, 0)
        self.assertEqual(code, client.EXIT_TIMEOUT)
        self.assertIsNone(result)
        self.assertTrue(process.killed)
        self.assertGreaterEqual(process.wait_calls, 2)

    def test_detached_unknown_session_identity_is_rejected(self) -> None:
        with mock.patch.object(sys, "argv", ["claude_cli_client.py", "--detach", "--continue", "prompt"]):
            self.assertEqual(client.main(), client.EXIT_BAD_ARGS)


if __name__ == "__main__":
    unittest.main()
