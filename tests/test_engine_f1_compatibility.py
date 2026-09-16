"""Opt-in frozen Engine loader composition; synthetic loopback peer, no live tasks.

Set CODEX_F1_ENGINE_ROOT to a clean checkout of ENGINE_REVISION. This is a test
input only: the client has no Engine runtime dependency. Run with python -B.
"""
from __future__ import annotations

import asyncio
import gc
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

ENGINE_REVISION = "830cf47e7d7a181879f773ac45b7766e8349bbe6"
REPO = Path(__file__).resolve().parents[1]
CLIENT_RELATIVE = "skills/codex-ws-client/scripts/codex_ws_client.py"
CLIENT = REPO / CLIENT_RELATIVE
ENGINE_ROOT = os.environ.get("CODEX_F1_ENGINE_ROOT")


@unittest.skipUnless(ENGINE_ROOT, "set CODEX_F1_ENGINE_ROOT for frozen Engine composition")
class EngineF1CompatibilityTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = Path(ENGINE_ROOT).resolve()
        revision = subprocess.check_output(["git", "-C", str(cls.engine), "rev-parse", "HEAD"], text=True).strip()
        if revision != ENGINE_REVISION:
            raise AssertionError(f"Expected frozen Engine {ENGINE_REVISION}, got {revision}")
        cls.source = cls.engine / "operating/governed-repository-work-dispatcher/dev/impl-harness-role-instance-provisioner/src"
        sys.path[:0] = [str(cls.source), str(cls.engine / "src")]
        cls.cli = importlib.import_module("harness_role_instance_provisioner.general_launch_cli")
        cls.adapters = importlib.import_module("harness_role_instance_provisioner.general_launch_adapters")

    def configured(self, root, endpoint, client_path=CLIENT):
        config = {
            "schema": "general-task-launch-config/v2",
            "engine_source": {"root": str(self.engine), "revision": ENGINE_REVISION},
            "installation_anchor": str(root / "fixture-anchor.json"),
            "ledger_root": str(root / "fixture-ledger"),
            "host": {"host_id": "synthetic", "server_identity": "synthetic",
                     "endpoint": endpoint, "client_path": str(client_path),
                     "client_sha256": hashlib.sha256(client_path.read_bytes()).hexdigest(),
                     "capabilities": {}},
        }
        path = root / "fixture-config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        # Real configured composition: no injected runtime, workspace, or connection.
        runtime = self.cli.configured(path).runtime
        # SQLite context managers commit but do not close. Collect the frozen
        # loader's unreferenced initialization connection before Windows cleanup.
        gc.collect()
        return runtime

    def retain(self, name, frames, observation=None):
        location = os.environ.get("CODEX_F1_EVIDENCE_DIR")
        if location:
            root = Path(location)
            root.mkdir(parents=True, exist_ok=True)
            with (root / f"{name}.json").open("x", encoding="utf-8") as stream:
                json.dump({"engine_revision": ENGINE_REVISION,
                           "client_sha256": hashlib.sha256(CLIENT.read_bytes()).hexdigest(),
                           "case": name, "frames": frames, "observation": observation}, stream, indent=2)

    async def test_prior_client_reproduces_capability_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prior = root / "prior_client.py"
            prior.write_bytes(subprocess.check_output(
                ["git", "-C", str(REPO), "show", f"92d3112:{CLIENT_RELATIVE}"]))
            runtime = self.configured(root, "ws://127.0.0.1:1", prior)
            with self.assertRaisesRegex(ValueError, "lacks structured RPC-error capability"):
                runtime._client_module()
        self.retain("prior-client-gate-rejected", [])

    async def test_real_loader_preserves_rpc_error_then_explicit_read_and_omission(self):
        frames, failures = [], []
        error = {"code": -32600, "message": "synthetic not materialized",
                 "data": {"nested": ["retain", 17], "retry": False}}

        async def handle(ws):
            try:
                async for raw in ws:
                    message = json.loads(raw)
                    frames.append(message)
                    if message["method"] == "initialized":
                        continue
                    if message["id"] == "read-error":
                        response = {"id": message["id"], "error": error}
                    else:
                        response = {"id": message["id"], "result": {"ok": True}}
                    await ws.send(json.dumps(response))
            except Exception as exc:
                failures.append(exc)

        with tempfile.TemporaryDirectory() as directory:
            async with serve(handle, "127.0.0.1", 0) as server:
                endpoint = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
                runtime = self.configured(Path(directory), endpoint)
                module = runtime._client_module()
                self.assertEqual(Path(module.__file__).resolve(), CLIENT.resolve())
                with mock.patch.object(module.sys, "argv", ["client", "--thread-id", "synthetic"]):
                    args = module.parse_args()
                async with runtime._connection() as connection:
                    self.assertTrue(connection.profile.preserve_rpc_errors)
                    await connection.initialize()
                    with self.assertRaises(self.adapters.ProviderRpcError) as caught:
                        await runtime.send(connection, "thread/read", {"threadId": "synthetic"}, "read-error")
                    expected = {"id": "read-error", "error": error}
                    self.assertEqual(caught.exception.provider_error,
                                     {"method": "thread/read", "response": expected})
                    self.assertIsInstance(caught.exception.__cause__, module.BoundedRpcError)
                    self.assertEqual(caught.exception.__cause__.to_dict()["rpc_response"], expected)
                    # No automatic retry. The caller explicitly chooses a fresh-id read.
                    self.assertEqual(sum(f["method"] == "thread/read" for f in frames), 1)
                    self.assertEqual(await runtime.send(connection, "thread/read", {"threadId": "synthetic"},
                                                        "read-after-error"), {"ok": True})
                    with mock.patch.object(module, "resolve_default_model", side_effect=AssertionError("resume default")):
                        resume = module.make_thread_params(args, None, args.instructions, include_sandbox=False,
                                                           include_project=False, creation=False, exclude_turns=True)
                        resume["threadId"] = "synthetic"
                        turn = module.make_turn_params(args, "synthetic", None, "synthetic only")
                    self.assertEqual(resume, {"threadId": "synthetic", "excludeTurns": True})
                    self.assertEqual(set(turn), {"threadId", "input"})
                    await runtime.send(connection, "thread/resume", resume, "resume-omission")
                    await runtime.send(connection, "turn/start", turn, "turn-omission")
        self.assertFalse(failures, failures)
        self.assertEqual([f["method"] for f in frames],
                         ["initialize", "initialized", "thread/read", "thread/read", "thread/resume", "turn/start"])
        self.retain("real-loader-error-read-omission", frames, caught.exception.provider_error)

    async def test_real_loader_malformed_and_approval_messages_still_fail_closed(self):
        for mode in ("malformed-error", "wrong-id", "server-approval", "duplicate-id"):
            with self.subTest(mode=mode):
                frames, failures = [], []
                closed = asyncio.Event()

                async def handle(ws):
                    try:
                        async for raw in ws:
                            message = json.loads(raw)
                            frames.append(message)
                            if message["method"] == "initialized":
                                continue
                            if message["method"] == "initialize":
                                response = {"id": message["id"], "result": {}}
                            elif mode == "server-approval":
                                response = {"id": "approval", "method": "item/commandExecution/requestApproval",
                                            "params": {"threadId": "synthetic", "turnId": "turn", "itemId": "cmd"}}
                            elif mode == "wrong-id":
                                response = {"id": "unrelated", "error": {"code": -1, "message": "wrong"}}
                            else:
                                response = {"id": message["id"], "error": {
                                    "code": True if mode == "malformed-error" else -32600, "message": "synthetic"}}
                            await ws.send(json.dumps(response))
                    except ConnectionClosed:
                        pass
                    except Exception as exc:
                        failures.append(exc)
                    finally:
                        closed.set()

                with tempfile.TemporaryDirectory() as directory:
                    async with serve(handle, "127.0.0.1", 0) as server:
                        runtime = self.configured(Path(directory), f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
                        module = runtime._client_module()
                        async with runtime._connection() as connection:
                            await connection.initialize()
                            if mode == "duplicate-id":
                                with self.assertRaises(self.adapters.ProviderRpcError):
                                    await runtime.send(connection, "thread/read", {}, "request-1")
                            with self.assertRaises(module.BoundedProtocolError):
                                await runtime.send(connection, "thread/read", {}, "request-1")
                            await asyncio.wait_for(closed.wait(), 2)
                self.assertFalse(failures, failures)
                self.assertEqual([f["method"] for f in frames], ["initialize", "initialized", "thread/read"])
                self.retain(mode, frames)


if __name__ == "__main__":
    unittest.main()
