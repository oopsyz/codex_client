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
                        "--out", cls.schema_dir.name], check=True, capture_output=True)
        root = Path(cls.schema_dir.name) / "v2"
        cls.schemas = {
            method: json.loads((root / name).read_text(encoding="utf-8"))
            for method, name in (("thread/resume", "ThreadResumeParams.json"),
                                 ("thread/start", "ThreadStartParams.json"),
                                 ("turn/start", "TurnStartParams.json"))
        }

    async def exercise(self, options, prompts=(), idle=0):
        calls, failures = [], []

        async def handle(ws):
            try:
                async for raw in ws:
                    request = json.loads(raw)
                    method = request["method"]
                    if method == "initialized":
                        continue
                    calls.append(request)
                    if method in self.schemas:
                        validate(request["params"], self.schemas[method])
                    if method == "initialize":
                        result = {"userAgent": "synthetic-model-test"}
                    elif method == "thread/read":
                        result = {"thread": {"id": "existing", "updatedAt": time.time() - idle}}
                    elif method in ("thread/start", "thread/resume"):
                        result = {"thread": {"id": "fresh" if method == "thread/start" else "existing",
                                             "status": {"type": "idle"}}}
                    elif method == "turn/start":
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
                                                     "--cwd", temp, *options]):
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
        return [(c["method"], c["params"]) for c in calls if c["method"] != "initialize"], resolved

    async def test_resume_omits_model_and_reasoning_in_all_modes(self):
        for mode in ([], ["--detach"], ["--repl"]):
            with self.subTest(mode=mode):
                calls, resolved = await self.exercise(["--thread-id", "existing", *mode, "continue"],
                                                       ["continue", "/exit"])
                self.assertEqual(calls[0][0], "thread/resume")
                self.assertTrue(any(method == "turn/start" for method, _ in calls))
                for _, params in calls:
                    self.assertNotIn("model", params)
                    self.assertNotIn("effort", params)
                    self.assertNotIn("config", params)
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
                turn = next(params for method, params in calls if method == "turn/start")
                self.assertNotIn("model", turn)
                self.assertNotIn("effort", turn)
                self.assertEqual(resolved, 1)

    async def test_repl_new_after_resume_uses_creation_default(self):
        calls, resolved = await self.exercise(["--thread-id", "existing", "--permissions", "fixture", "--repl"],
                                               ["/new", "hello", "/exit"])
        self.assertEqual([method for method, _ in calls],
                         ["thread/resume", "thread/start", "turn/start"])
        self.assertNotIn("model", calls[0][1])
        self.assertEqual(calls[1][1]["model"], "configured-luna")
        self.assertEqual(resolved, 1)

    async def test_ttl_only_resolves_default_when_creating(self):
        for idle, expected in ((0, "thread/resume"), (120, "thread/start")):
            with self.subTest(idle=idle):
                calls, resolved = await self.exercise(["--thread-id", "existing", "--permissions", "fixture",
                                                       "--resume-ttl", "60", "continue"], idle=idle)
                self.assertEqual(calls[1][0], expected)
                if expected == "thread/start":
                    self.assertEqual(calls[1][1]["model"], "configured-luna")
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


if __name__ == "__main__":
    unittest.main()
