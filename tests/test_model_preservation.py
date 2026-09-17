"""Real CLI/WebSocket serialization against a synthetic peer, never a live task."""
from __future__ import annotations

import asyncio
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from jsonschema import validate
from websockets.asyncio.server import serve

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/codex-ws-client/scripts"))
import codex_ws_client as client


class ModelPreservationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema_dir = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.schema_dir.cleanup)
        subprocess.run(["cmd", "/c", "codex", "app-server", "generate-json-schema",
                        "--experimental", "--out", cls.schema_dir.name], check=True, capture_output=True)
        root = Path(cls.schema_dir.name) / "v2"
        cls.schemas = {
            method: json.loads((root / name).read_text(encoding="utf-8"))
            for method, name in (("thread/resume", "ThreadResumeParams.json"),
                                 ("thread/start", "ThreadStartParams.json"),
                                 ("turn/start", "TurnStartParams.json"))
        }

    async def exercise(self, options, prompts=(), idle=0, approvals=False, cwd=True):
        calls, failures = [], []
        approval_replies = []

        async def handle(ws):
            try:
                async for raw in ws:
                    request = json.loads(raw)
                    method = request["method"]
                    if method == "initialized":
                        continue
                    calls.append(request)
                    if method in self.schemas:
                        self.assertLessEqual(set(request["params"]), set(self.schemas[method]["properties"]))
                        validate(request["params"], self.schemas[method])
                    if method == "initialize":
                        result = {"userAgent": "synthetic-model-test"}
                    elif method == "thread/read":
                        result = {"thread": {"id": "existing", "updatedAt": time.time() - idle}}
                    elif method in ("thread/start", "thread/resume"):
                        result = {"thread": {"id": "fresh" if method == "thread/start" else "existing",
                                             "status": {"type": "idle"}}}
                    elif method == "turn/start":
                        if approvals:
                            requests = [
                                ("item/commandExecution/requestApproval", {}, {"decision": "decline"}),
                                ("item/fileChange/requestApproval", {}, {"decision": "decline"}),
                                ("item/permissions/requestApproval", {"permissions": {"network": {"enabled": True}}},
                                 {"permissions": {}, "scope": "turn"}),
                            ]
                            for index, (approval_method, params, expected) in enumerate(requests):
                                req_id = f"approval-{index}"
                                await ws.send(json.dumps({"jsonrpc": "2.0", "id": req_id,
                                                          "method": approval_method, "params": params}))
                                reply = json.loads(await ws.recv())
                                self.assertEqual(reply["id"], req_id)
                                self.assertEqual(reply["result"], expected)
                                approval_replies.append(reply)
                        result = {"turn": {"id": "turn-fixture", "status": "inProgress"}}
                    elif method == "thread/unsubscribe":
                        result = {"status": "unsubscribed"}
                    else:
                        raise AssertionError(f"Unexpected method: {method}")
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}))
                    if method == "turn/start":
                        await ws.send(json.dumps({"jsonrpc": "2.0", "method": "turn/completed",
                                                  "params": {"threadId": request["params"]["threadId"],
                                                             "turn": {"id": "turn-fixture", "status": "completed"}}}))
            except Exception as exc:
                failures.append(exc)
                await ws.close()

        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config.toml"
            config.write_text('model = "configured-luna"\nmodel_reasoning_effort = "low"\n', encoding="utf-8")
            async with serve(handle, "127.0.0.1", 0) as server:
                uri = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
                with mock.patch.object(sys, "argv", ["client", "--uri", uri, "--json", "--timeout", "2",
                                                     *(["--cwd", temp] if cwd else []), *options]):
                    args = client.parse_args()
                stdout, stderr = io.StringIO(), io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr), \
                     mock.patch.object(client, "_codex_config_path", return_value=config), \
                     mock.patch.object(client, "resolve_default_model", wraps=client.resolve_default_model) as resolve, \
                     mock.patch.object(client, "install_sigint_handler"), \
                     mock.patch("builtins.input", side_effect=prompts):
                    code = await asyncio.wait_for(client.run_client(args), 5)
                if failures:
                    raise failures[0]
                self.assertEqual(code, client.EXIT_SUCCESS, stderr.getvalue())
                resolved = resolve.call_count
        if failures:
            raise failures[0]
        if approvals:
            self.assertEqual(len(approval_replies), 3)
        return [(c["method"], c["params"]) for c in calls if c["method"] != "initialize"], resolved

    async def test_resume_omits_model_and_reasoning_in_all_modes(self):
        for mode in ([], ["--detach"], ["--repl"]):
            with self.subTest(mode=mode):
                calls, resolved = await self.exercise(["--thread-id", "existing", *mode, "continue"],
                                                       ["continue", "/exit"])
                self.assertEqual(calls[0][0], "thread/resume")
                self.assertTrue(any(method == "turn/start" for method, _ in calls))
                for method, params in calls:
                    for field in ("model", "effort", "config", "personality", "developerInstructions",
                                  "ephemeral", "runtimeWorkspaceRoots", "permissions"):
                        self.assertNotIn(field, params)
                    if method in ("thread/resume", "turn/start"):
                        self.assertEqual(params["approvalPolicy"], "on-request")
                        self.assertEqual(params["approvalsReviewer"], "auto_review")
                self.assertEqual(resolved, 0)

    async def test_explicit_overrides_in_all_modes(self):
        for mode in ([], ["--detach"], ["--repl"]):
            with self.subTest(mode=mode):
                calls, resolved = await self.exercise(["--thread-id", "existing", "--model", "explicit-astra",
                                                       "--effort", "medium", *mode, "continue"],
                                                       ["continue", "/exit"])
                self.assertEqual(calls[0][1]["model"], "explicit-astra")
                turn = next(params for method, params in calls if method == "turn/start")
                self.assertEqual(turn["model"], "explicit-astra")
                self.assertEqual(turn["effort"], "medium")
                self.assertEqual(resolved, 0)

    async def test_model_and_effort_overrides_are_independent(self):
        for override, present, absent in ((["--model", "explicit-astra"], "model", "effort"),
                                          (["--effort", "high"], "effort", "model")):
            calls, resolved = await self.exercise(["--thread-id", "existing", *override, "continue"])
            turn = next(params for method, params in calls if method == "turn/start")
            self.assertIn(present, turn)
            self.assertNotIn(absent, turn)
            self.assertEqual(resolved, 0)

    async def test_new_threads_use_configured_model_in_all_modes(self):
        for mode in ([], ["--detach"], ["--repl"]):
            with self.subTest(mode=mode):
                calls, resolved = await self.exercise(["--sandbox", "read-only", *mode, "hello"],
                                                       ["hello", "/exit"])
                self.assertEqual(calls[0][0], "thread/start")
                self.assertEqual(calls[0][1]["model"], "configured-luna")
                self.assertEqual(calls[0][1]["personality"], "pragmatic")
                self.assertEqual(calls[0][1]["developerInstructions"], "Answer concisely.")
                self.assertEqual(calls[0][1]["approvalPolicy"], "never")
                self.assertFalse(calls[0][1]["ephemeral"])
                turn = next(params for method, params in calls if method == "turn/start")
                self.assertNotIn("model", turn)
                self.assertNotIn("effort", turn)
                self.assertNotIn("approvalPolicy", turn)
                self.assertNotIn("personality", turn)
                self.assertEqual(resolved, 1)

    async def test_repl_new_after_resume_uses_creation_default(self):
        calls, resolved = await self.exercise(["--thread-id", "existing", "--permissions", "fixture", "--repl"],
                                               ["/new", "hello", "/exit"])
        self.assertEqual([method for method, _ in calls],
                         ["thread/resume", "thread/start", "turn/start"])
        self.assertNotIn("model", calls[0][1])
        self.assertEqual(calls[1][1]["model"], "configured-luna")
        self.assertEqual(calls[1][1]["approvalPolicy"], "never")
        self.assertEqual(calls[1][1]["personality"], "pragmatic")
        self.assertEqual(calls[1][1]["developerInstructions"], "Answer concisely.")
        self.assertEqual(resolved, 1)

    async def test_ttl_only_resolves_default_when_creating(self):
        for idle, expected in ((0, "thread/resume"), (120, "thread/start")):
            with self.subTest(idle=idle):
                calls, resolved = await self.exercise(["--thread-id", "existing", "--permissions", "fixture",
                                                       "--resume-ttl", "60", "continue"], idle=idle)
                self.assertEqual(calls[1][0], expected)
                if expected == "thread/start":
                    self.assertEqual(calls[1][1]["model"], "configured-luna")
                    self.assertEqual(calls[1][1]["approvalPolicy"], "never")
                    self.assertEqual(calls[1][1]["personality"], "pragmatic")
                    self.assertEqual(calls[1][1]["developerInstructions"], "Answer concisely.")
                    self.assertEqual(resolved, 1)
                else:
                    self.assertNotIn("model", calls[1][1])
                    self.assertEqual(resolved, 0)

    async def test_new_and_ttl_threads_honor_explicit_overrides(self):
        for options in (["--sandbox", "read-only"],
                        ["--thread-id", "existing", "--permissions", "fixture", "--resume-ttl", "60"]):
            calls, resolved = await self.exercise([*options, "--model", "explicit-astra",
                                                   "--effort", "medium", "hello"], idle=120)
            for method, params in calls:
                if method in ("thread/start", "turn/start"):
                    self.assertEqual(params["model"], "explicit-astra")
                if method == "turn/start":
                    self.assertEqual(params["effort"], "medium")
            self.assertEqual(resolved, 0)

    async def test_explicit_resume_overrides_and_noninteractive_denials(self):
        for mode in ([], ["--detach"], ["--repl"]):
            with self.subTest(mode=mode):
                calls, _ = await self.exercise(
                    ["--thread-id", "existing", "--personality", "friendly", "--instructions", "Do the assigned work.",
                     "--approval-policy", "on-request", *mode, "continue"],
                    ["continue", "/exit"], approvals=True)
                resume = calls[0][1]
                self.assertEqual(resume["developerInstructions"], "Do the assigned work.")
                for method, params in calls:
                    if method in ("thread/resume", "turn/start"):
                        self.assertEqual(params["personality"], "friendly")
                        self.assertEqual(params["approvalPolicy"], "on-request")
                        self.assertNotIn("approvalsReviewer", params)
                        self.assertNotIn("ephemeral", params)
                    if method == "turn/start":
                        self.assertNotIn("developerInstructions", params)

    async def test_omitted_resume_policy_uses_approve_for_me_defaults(self):
        for mode in ([], ["--detach"], ["--repl"], ["--interactive-approvals"],
                     ["--detach", "--interactive-approvals"],
                     ["--repl", "--interactive-approvals"]):
            with self.subTest(mode=mode):
                prompts = (
                    ["continue", "d", "d", "d", "/exit"]
                    if mode == ["--repl", "--interactive-approvals"]
                    else ["continue", "/exit"]
                )
                calls, _ = await self.exercise(["--thread-id", "existing", *mode, "continue"],
                                               prompts, approvals=True, cwd=False)
                for method, params in calls:
                    if method in ("thread/resume", "turn/start"):
                        self.assertEqual(params["approvalPolicy"], "on-request")
                        if mode == ["--repl", "--interactive-approvals"]:
                            self.assertNotIn("approvalsReviewer", params)
                        else:
                            self.assertEqual(params["approvalsReviewer"], "auto_review")
                    self.assertNotIn("cwd", params)

    async def test_interactive_repl_policy_and_explicit_precedence(self):
        for policy, expected in (([], "on-request"), (["--approval-policy", "never"], "never")):
            for target in (["--thread-id", "existing"], ["--sandbox", "read-only"]):
                with self.subTest(policy=policy, target=target):
                    calls, _ = await self.exercise([*target, "--repl", "--interactive-approvals", *policy],
                                                   ["continue", "d", "d", "d", "/exit"], approvals=True)
                    for method, params in calls:
                        if method in ("thread/resume", "thread/start", "turn/start"):
                            self.assertEqual(params["approvalPolicy"], expected)
                            self.assertNotIn("approvalsReviewer", params)

    async def test_explicit_cwd_roots_profile_remain_on_supported_requests(self):
        calls, _ = await self.exercise(["--thread-id", "existing", "--permissions", "fixture",
                                       "--runtime-workspace-root", "C:/fixture", "continue"])
        for method, params in calls:
            if method in ("thread/resume", "turn/start"):
                self.assertIn("cwd", params)
                self.assertEqual(params["runtimeWorkspaceRoots"], ["C:/fixture"])
                if method == "turn/start":
                    self.assertEqual(params["permissions"], "fixture")
                else:
                    self.assertNotIn("permissions", params)

    async def test_explicit_empty_instructions_are_not_replaced(self):
        calls, _ = await self.exercise(["--thread-id", "existing", "--instructions", "", "continue"])
        self.assertEqual(calls[0][1]["developerInstructions"], "")

    async def test_fresh_explicit_settings_and_approval_safety_in_all_modes(self):
        for mode in ([], ["--detach"], ["--repl"]):
            with self.subTest(mode=mode):
                calls, _ = await self.exercise(
                    ["--sandbox", "read-only", "--personality", "friendly", "--instructions", "Explicit task.",
                     "--approval-policy", "on-request", *mode, "hello"], ["hello", "/exit"], approvals=True)
                self.assertEqual(calls[0][1]["developerInstructions"], "Explicit task.")
                for method, params in calls:
                    if method in ("thread/start", "turn/start"):
                        self.assertEqual(params["personality"], "friendly")
                        self.assertEqual(params["approvalPolicy"], "on-request")

    async def test_ephemeral_is_creation_only(self):
        calls, _ = await self.exercise(["--sandbox", "read-only", "--ephemeral", "hello"])
        self.assertTrue(calls[0][1]["ephemeral"])
        with mock.patch.object(sys, "argv", ["client", "--thread-id", "existing", "--ephemeral", "continue"]):
            args = client.parse_args()
        with mock.patch.object(client.websockets, "connect", side_effect=AssertionError("must reject before connect")), \
             redirect_stderr(io.StringIO()):
            self.assertEqual(await client.run_client(args), client.EXIT_BAD_ARGS)


if __name__ == "__main__":
    unittest.main()
