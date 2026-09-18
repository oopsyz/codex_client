"""CLI-to-WebSocket project/import contract tests; no real Codex task or model."""
from __future__ import annotations

import asyncio
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from jsonschema import validate
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/codex-ws-client/scripts"))
import codex_ws_client as client

# npm @openai/codex 0.154.0 Windows x64, generate-json-schema --experimental.
IMPORT_SCHEMA_MANIFEST = {
    "ProjectImportParams.json": "c26f8dd7b44d8a2881100c5f04b39328da71fca9b251b32f2e3fc830c6fe46c9",
    "ProjectImportResponse.json": "0d7ee068819bee4a6910a7aac7168963e1c99fdf0998de5f36f62f12473507d7",
}


class ProjectImportEndToEndTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        root = Path(cls.tmp.name)
        subprocess.run(
            ["cmd", "/c", "codex", "app-server", "generate-json-schema",
             "--experimental", "--out", str(root)], check=True,
        )
        cls.schemas = {
            name: json.loads((root / "v2" / name).read_text(encoding="utf-8"))
            for name in IMPORT_SCHEMA_MANIFEST
        }

    def args(self, uri, extra=()):
        with mock.patch.object(sys, "argv", [
            "codex_ws_client.py", "--uri", uri, "--json", "--timeout", "2",
            "--connect-timeout", "2", "--import-project", "Imported",
            "--project-root", "/srv/project", "--project-thread", "thread-fixture",
            "--project-idempotency-key", "import-attempt-1",
            "--project-metadata", "owner=test", *extra,
        ]):
            return client.parse_args()

    async def run_cli(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr), \
             mock.patch.object(client, "resolve_default_model", return_value="fixture-only"), \
             mock.patch.object(client, "install_sigint_handler"):
            code = await asyncio.wait_for(client.run_client(args), timeout=5)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_exact_installed_import_schema_pins(self):
        for name, expected in IMPORT_SCHEMA_MANIFEST.items():
            encoded = json.dumps(self.schemas[name], sort_keys=True, separators=(",", ":")).encode()
            self.assertEqual(hashlib.sha256(encoded).hexdigest(), expected, name)

    async def exercise(self, mode, repeats=1):
        calls, connections, failures = [], [], []
        response = {"project": {
            "id": "project-fixture", "name": "Imported",
            "roots": [{"path": "/srv/project"}], "metadata": {"owner": "test"},
            "position": 0, "createdAt": 1, "updatedAt": 1, "recencyAt": None,
        }}
        validate(response, self.schemas["ProjectImportResponse.json"])

        async def handle(ws):
            connection = []
            connections.append(connection)
            try:
                async for raw in ws:
                    message = json.loads(raw)
                    connection.append(message)
                    method = message["method"]
                    if method == "initialize":
                        self.assertTrue(message["params"]["capabilities"]["experimentalApi"])
                        await ws.send(json.dumps({"id": message["id"], "result": {"userAgent": "fixture"}}))
                    elif method == "initialized":
                        self.assertNotIn("id", message)
                    else:
                        self.assertEqual(method, "project/import")  # no thread/turn creation
                        self.assertEqual([m["method"] for m in connection[:2]], ["initialize", "initialized"])
                        validate(message["params"], self.schemas["ProjectImportParams.json"])
                        calls.append(message)
                        if mode == "disconnect":
                            await ws.close()
                            return
                        if mode == "success":
                            # Reference projects.rs emits committed membership notifications first.
                            for notification in [
                                {"method": "project/changed", "params": {
                                    "projectId": "project-fixture", "changeType": "created"}},
                                {"method": "thread/project/updated", "params": {
                                    "threadId": "thread-fixture", "projectId": "project-fixture"}},
                            ]:
                                await ws.send(json.dumps(notification))
                            answer = {"id": message["id"], "result": response}
                        else:
                            answer = {"id": message["id"], "error": {
                                "code": client.APP_SERVER_OVERLOADED if mode == "overload" else -32602,
                                "message": mode,
                            }}
                        await ws.send(json.dumps(answer))
            except ConnectionClosed as exc:
                # run_client unwinds its socket context before formatting RPC errors.
                if mode not in ("overload", "invalid-threads"):
                    failures.append(exc)
            except Exception as exc:
                failures.append(exc)
                await ws.close()

        async with serve(handle, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            results = [await self.run_cli(self.args(f"ws://127.0.0.1:{port}")) for _ in range(repeats)]
        if failures:
            raise failures[0]
        self.assertEqual(len(calls), repeats)  # neither overload nor unknown outcome retries
        self.assertEqual(len(connections), repeats)
        for call in calls:
            self.assertEqual(call["params"], {
                "name": "Imported", "roots": [{"path": "/srv/project"}],
                "threads": ["thread-fixture"], "idempotencyKey": "import-attempt-1",
                "metadata": {"owner": "test"},
            })
        return results, response

    async def test_import_cli_wire_notifications_result_and_explicit_replay(self):
        results, response = await self.exercise("success", repeats=2)
        for code, stdout, stderr in results:
            self.assertEqual(code, client.EXIT_SUCCESS, stderr)
            self.assertEqual(json.loads(stdout), response)

    async def test_import_overload_is_error_without_automatic_retry(self):
        results, _ = await self.exercise("overload")
        code, stdout, _ = results[0]
        self.assertEqual(code, client.EXIT_TURN_FAILURE)
        self.assertEqual(json.loads(stdout)["status"], "error")

    async def test_import_server_rejection_is_not_success(self):
        results, _ = await self.exercise("invalid-threads")
        code, stdout, _ = results[0]
        self.assertEqual(code, client.EXIT_TURN_FAILURE)
        self.assertEqual(json.loads(stdout)["status"], "error")

    async def test_import_unknown_connection_outcome_is_not_retried(self):
        results, _ = await self.exercise("disconnect")
        code, stdout, stderr = results[0]
        self.assertEqual(code, client.EXIT_CONNECTION_FAILURE)
        failure = json.loads(stdout)
        self.assertEqual(failure["status"], "unknown")
        self.assertEqual(failure["error"]["kind"], "transport_closed")
        self.assertIn("WebSocket connection lost", failure["error"]["message"])
        self.assertEqual(stderr, "")

    async def test_invalid_or_ambiguous_inputs_fail_before_connect(self):
        changes = [
            {"create_project": "Other"},
            {"import_project": "", "create_project": "Other"},
            {"import_project": ""},
            {"import_project": " "},
            {"project_root": []},
            {"project_thread": []},
            {"project_thread": [" "]},
            {"project_idempotency_key": ""},
            {"project_metadata": ["malformed"]},
        ]
        for change in changes:
            with self.subTest(change=change):
                args = self.args("ws://127.0.0.1:1")
                for key, value in change.items():
                    setattr(args, key, value)
                with mock.patch.object(client.websockets, "connect", side_effect=AssertionError("network")):
                    code, stdout, _ = await self.run_cli(args)
                self.assertEqual(code, client.EXIT_BAD_ARGS)
                self.assertEqual(stdout, "")


if __name__ == "__main__":
    unittest.main()
