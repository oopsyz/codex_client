"""Explicit WebSocket size diagnostics; loopback fixtures only, no Codex model."""
from __future__ import annotations

import asyncio
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/codex-ws-client/scripts"))
import codex_ws_client as client


class CloseMessageTests(unittest.TestCase):
    def test_local_limit_with_no_peer_close(self):
        exc = ConnectionClosedError(None, Close(1009, "frame with 24932160 bytes exceeds limit of 8000000 bytes"))
        message = client.websocket_close_message(exc, 8_000_000)
        self.assertIn("client's 8000000-byte limit", message)
        self.assertIn("24932160", message)
        self.assertIn("--thread-turns", message)

    def test_local_limit_with_peer_echo(self):
        exc = ConnectionClosedError(Close(1009, ""), Close(1009, "too big"), False)
        self.assertIn("Incoming", client.websocket_close_message(exc, 8_000_000))

    def test_peer_limit_is_not_attributed_to_client(self):
        exc = ConnectionClosedError(Close(1009, "peer limit"), Close(1009, "peer limit"), True)
        message = client.websocket_close_message(exc, 8_000_000)
        self.assertIn("outgoing", message)
        self.assertIn("server-side limit", message)
        self.assertNotIn("8000000", message)

    def test_other_closures_keep_original_diagnostic(self):
        exc = ConnectionClosedError(None, None)
        self.assertEqual(client.websocket_close_message(exc, 8_000_000), f"WebSocket connection lost: {exc}")


class SizeErrorCliTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_oversized_history_in_text_and_json_modes(self):
        for json_mode in (False, True):
            with self.subTest(json_mode=json_mode):
                calls = []
                payload_size = []

                async def handle(ws):
                    request = json.loads(await ws.recv())
                    calls.append(request["method"])
                    await ws.send(json.dumps({"id": request["id"], "result": {"userAgent": "fixture"}}))
                    calls.append(json.loads(await ws.recv())["method"])
                    request = json.loads(await ws.recv())
                    calls.append(request["method"])
                    self.assertTrue(request["params"]["includeTurns"])
                    payload = json.dumps({"id": request["id"], "result": {"padding": "x" * 8_000_000}})
                    payload_size.append(len(payload.encode()))
                    await ws.send(payload)
                    await ws.wait_closed()

                async with serve(handle, "127.0.0.1", 0, compression=None) as server:
                    port = server.sockets[0].getsockname()[1]
                    argv = ["client", "--uri", f"ws://127.0.0.1:{port}", "--timeout", "3",
                            "--read-thread", "fixture-thread", "--include-turns"]
                    if json_mode:
                        argv.append("--json")
                    with mock.patch.object(sys, "argv", argv):
                        args = client.parse_args()
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with redirect_stdout(stdout), redirect_stderr(stderr), \
                         mock.patch.object(client, "install_sigint_handler"):
                        code = await asyncio.wait_for(client.run_client(args), timeout=10)

                self.assertEqual(code, client.EXIT_CONNECTION_FAILURE)
                self.assertEqual(calls, ["initialize", "initialized", "thread/read"])
                if json_mode:
                    result = json.loads(stdout.getvalue())
                    self.assertEqual(result["status"], "unknown")
                    self.assertEqual(result["error"]["kind"], "transport_closed")
                    message = result["error"]["message"]
                else:
                    self.assertEqual(stdout.getvalue(), "")
                    message = stderr.getvalue()
                self.assertIn("Incoming App Server WebSocket message", message)
                self.assertIn("8000000-byte limit", message)
                self.assertIn("1009", message)
                self.assertIn(str(payload_size[0]), message)
                self.assertIn("--include-turns", message)


if __name__ == "__main__":
    unittest.main()
