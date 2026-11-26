"""Exercise saved-task settings preservation through the CLI and WebSocket."""

import asyncio
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/codex-ws-client/scripts"))
import codex_ws_client as client


TASK = "saved-task"
PROJECT = "saved-project"
CWD = "/srv/projects/saved"
THREAD = {"id": TASK, "projectId": PROJECT, "cwd": CWD, "ephemeral": False}
SCHEMA = {"type": "object"}
PRESERVE = "--preserve-existing-task-settings"
ASSERTIONS = ["--expect-cwd", CWD, "--expect-project-id", PROJECT]


class ExistingTaskSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, options, resume_response, *, overload_at=None):
        calls = []

        async def peer(socket):
            while True:
                try:
                    message = await socket.recv()
                except ConnectionClosed:
                    return
                request = json.loads(message)
                method = request["method"]
                if method == "initialized":
                    continue
                calls.append((method, request["params"]))
                if method == overload_at:
                    await socket.send(json.dumps({"id": request["id"], "error": {
                        "code": client.APP_SERVER_OVERLOADED, "message": "fixture overload",
                    }}))
                    continue
                responses = {
                    "initialize": {},
                    "thread/resume": resume_response,
                    "turn/start": {"turn": {"id": "saved-turn", "status": "inProgress"}},
                    "thread/unsubscribe": {"status": "unsubscribed"},
                }
                await socket.send(json.dumps({"id": request["id"], "result": responses[method]}))
                if method == "turn/start" and "--detach" not in options:
                    await socket.send(json.dumps({"method": "turn/completed", "params": {
                        "threadId": TASK, "turn": {"id": "saved-turn", "status": "completed"},
                    }}))

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(client, "install_sigint_handler"):
            prompt = Path(tmp) / "prompt.txt"
            prompt.write_text("Use the supplied reference.", encoding="utf-8")
            async with serve(peer, "127.0.0.1", 0) as server:
                uri = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
                argv = ["client", "--uri", uri, "--json", "--no-stream",
                        "--prompt-file", str(prompt), "--output-schema", json.dumps(SCHEMA), *options]
                with mock.patch.object(sys, "argv", argv):
                    args = client.parse_args()
                stdout, stderr = io.StringIO(), io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = await asyncio.wait_for(client.run_client(args), 5)
        return code, calls, stdout.getvalue(), stderr.getvalue()

    async def test_preserve_sends_only_saved_identity_prompt_and_schema(self):
        for mode in ([], ["--detach"]):
            with self.subTest(mode=mode):
                code, calls, stdout, stderr = await self.exercise(
                    ["--thread-id", TASK, PRESERVE, *ASSERTIONS, "--resume-ttl", "0", *mode],
                    {"thread": THREAD, "cwd": CWD, "approvalPolicy": "untrusted",
                     "approvalsReviewer": "user", "sandbox": {"type": "workspaceWrite"}},
                )
                self.assertEqual(code, client.EXIT_SUCCESS, stderr)
                expected_calls = [
                    ("thread/resume", {"threadId": TASK, "excludeTurns": True}),
                    ("turn/start", {"threadId": TASK, "input": [{"type": "text", "text": "Use the supplied reference."}],
                                    "outputSchema": SCHEMA}),
                ]
                if mode:
                    expected_calls.append(("thread/unsubscribe", {"threadId": TASK}))
                self.assertEqual(calls[1:], expected_calls)
                self.assertEqual(json.loads(stdout)["status"], "detached" if mode else "completed")

    async def test_mismatched_or_missing_assignment_stops_before_turn(self):
        responses = [
            {"thread": None}, {"thread": {}},
            {"thread": {**THREAD, "id": "another-task"}},
            {"thread": {**THREAD, "projectId": None}},
            {"thread": {**THREAD, "projectId": "another-project"}},
            {"thread": {**THREAD, "cwd": CWD + "/."}},
            {"thread": {**THREAD, "ephemeral": True}},
            {"thread": THREAD, "cwd": "/effective-cwd-mismatch"},
        ]
        for response in responses:
            with self.subTest(response=response):
                code, calls, stdout, _ = await self.exercise(["--thread-id", TASK, PRESERVE, *ASSERTIONS], response)
                self.assertEqual(code, client.EXIT_BAD_ARGS)
                self.assertEqual([method for method, _ in calls], ["initialize", "thread/resume"])
                self.assertEqual(json.loads(stdout)["error"]["kind"], "invalid_arguments")

    async def test_invalid_or_conflicting_options_stop_before_connecting(self):
        bound = ["--thread-id", TASK, PRESERVE, *ASSERTIONS]
        options = [
            [*ASSERTIONS], [PRESERVE, *ASSERTIONS], ["--thread-id", TASK, PRESERVE],
            [*bound, "--expect-cwd", "relative/path"], [*bound, "--expect-cwd", CWD + "/../saved"],
            [*bound, "--expect-project-id", ""], [*bound, "--read-thread", TASK],
            [*bound, "--project-id", PROJECT], [*bound, "--cwd", CWD],
            [*bound, "--sandbox", "read-only"], [*bound, "--permissions", "profile"],
            [*bound, "--approval-policy", "never"], [*bound, "--runtime-workspace-root", CWD],
            [*bound, "--resume-ttl", "1"], [*bound, "--repl"], [*bound, "--interactive-approvals"],
            [*bound, "--ephemeral"], [*bound, "--model", "model"], [*bound, "--effort", "high"],
            [*bound, "--personality", "pragmatic"], [*bound, "--instructions", ""],
        ]
        for flags in options:
            with self.subTest(flags=flags), mock.patch.object(client.websockets, "connect") as connect:
                code, calls, _, _ = await self.exercise(flags, {"thread": THREAD})
                self.assertEqual(code, client.EXIT_BAD_ARGS)
                connect.assert_not_called()
                self.assertEqual(calls, [])

    async def test_preserve_does_not_repeat_resume_or_turn_after_overload(self):
        for method in ("thread/resume", "turn/start"):
            with self.subTest(method=method):
                code, calls, _, _ = await self.exercise(
                    ["--thread-id", TASK, PRESERVE, *ASSERTIONS], {"thread": THREAD}, overload_at=method,
                )
                self.assertEqual(code, client.EXIT_TURN_FAILURE)
                self.assertEqual([name for name, _ in calls].count(method), 1)
                self.assertEqual(calls[-1][0], method)
