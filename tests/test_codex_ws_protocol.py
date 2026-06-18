from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from jsonschema import validate
from types import SimpleNamespace

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "skills" / "codex-ws-client" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from codex_ws_client import (  # noqa: E402
    APP_SERVER_OVERLOADED,
    EXIT_SUCCESS,
    EXIT_SIGINT,
    _cancel,
    ProtocolClient,
    RetryConfig,
    RpcError,
    TurnDeadline,
    ensure_thread,
    run_detached_turn,
    run_turn,
    run_repl,
)

SCHEMA_MANIFEST = {
  "ClientNotification.json": "4446a1ae8626aa55d812836bfc2dae24213500d87c4759af2698f6199ea5b59f",
  "ClientRequest.json": "7aa39426900d503cf414ba9a3c109fcd0b2e53b32befeda13f03b64fa68cf44c",
  "JSONRPCError.json": "8835db6c4ada12ec628613fe2f571edc2c8fb55fc31bac706085e25ad1a08c0e",
  "ServerNotification.json": "8f5448857104f26dd9228e4d0f7cfbd9c6b4e136905ba7ce41a9f31b4f9e8d4e",
  "ServerRequest.json": "5279978a5f553b3c855f1c28281fdea7f3a79d6d6e1e4447ee952623dd56bf9c",
  "v2/ThreadListParams.json": "bc3b8aad80284111e5a4aebdd7f30530f45a41b3f93d15a8de52e7f40d89e94a",
  "v2/ThreadListResponse.json": "85bb34bb0e5df5cd2ff2b8ffdfe246e3e0a763c05ede1713e7e20becad436795",
  "v2/ThreadReadParams.json": "ab07c67662e3a8db06a9a2905af350919c7f9a7d1d1c4055a33c26234b6c9f23",
  "v2/ThreadReadResponse.json": "d7d614549519fe72ab6040a127a6f794528186d6335f29a6af2deca8c2c3c9f5",
  "v2/ThreadResumeParams.json": "0ffd678601ef5b7bcc88bcc0d730920fe39fda92398d325d661a4f324366bf2a",
  "v2/ThreadResumeResponse.json": "3e6f4d67012a04a12761521f36a52028f111870b634084b5df82c128c239376b",
  "v2/ThreadStartParams.json": "d722042f74bb3155a70b24315f87a0bffe4e5ee1c9a681e004d68e323bd2b94d",
  "v2/ThreadStartResponse.json": "aa34b960fe2636fe966604771d017b6108c71e86245bd02d24fe86b6ce64cfe7",
  "v2/TurnStartParams.json": "513aa54ff57ca8fdcaa631c0ca344820ae1e1f9c2a3a926a3a5e944e52c413f5",
  "v2/TurnStartResponse.json": "ea9ef97812cb55569d4287d812e9100c96e90cc36ad3df43813f2b7e2b79b041",
}


def _hash_schema(path: Path) -> str:
    normalized = json.dumps(json.loads(path.read_text(encoding="utf-8")), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class MockWebSocket:
    def __init__(self, messages: list[dict[str, object] | str]) -> None:
        self.messages = asyncio.Queue()
        for message in messages:
            self.messages.put_nowait(message if isinstance(message, str) else json.dumps(message))
        self.sent: list[dict[str, object]] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def recv(self) -> str:
        item = await self.messages.get()
        if isinstance(item, BaseException):
            raise item
        return item


def response_for(sent: dict[str, object], result: dict[str, object]) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": sent["id"], "result": result}


class ProtocolClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_thread_uses_structured_params(self) -> None:
        ws = MockWebSocket([])
        client = ProtocolClient(ws)
        args = SimpleNamespace(
            thread_id="thread-1",
            cwd=".",
            sandbox="read-only",
            model="gpt-5",
            personality="pragmatic",
            instructions="dev",
            ephemeral=False,
            print_thread_id=False,
        )

        async def recv() -> str:
            return json.dumps(response_for(ws.sent[-1], {"thread": {"id": "thread-1", "status": {"type": "idle"}}}))

        ws.recv = recv  # type: ignore[method-assign]
        thread_id, reused = await ensure_thread(client, args, "C:/repo", 1, 1)
        self.assertEqual((thread_id, reused), ("thread-1", True))
        self.assertEqual(ws.sent[0]["method"], "thread/resume")
        self.assertEqual(ws.sent[0]["params"]["cwd"], "C:/repo")

    async def test_resume_thread_omits_cwd_when_not_provided(self) -> None:
        ws = MockWebSocket([])
        client = ProtocolClient(ws)
        args = SimpleNamespace(
            thread_id="thread-1",
            cwd="",
            sandbox="read-only",
            model="gpt-5",
            personality="pragmatic",
            instructions="dev",
            ephemeral=False,
            print_thread_id=False,
        )

        async def recv() -> str:
            return json.dumps(response_for(ws.sent[-1], {"thread": {"id": "thread-1", "status": {"type": "idle"}}}))

        ws.recv = recv  # type: ignore[method-assign]
        thread_id, reused = await ensure_thread(client, args, None, 1, 1)
        self.assertEqual((thread_id, reused), ("thread-1", True))
        self.assertEqual(ws.sent[0]["method"], "thread/resume")
        self.assertNotIn("cwd", ws.sent[0]["params"])

    async def test_force_new_thread_ignores_existing_thread_id(self) -> None:
        class ThreadClient:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object], float | None]] = []

            async def start_thread(self, params: dict[str, object], timeout: float | None) -> dict[str, object]:
                self.calls.append(("start", params, timeout))
                return {"thread": {"id": "fresh-1"}}

            async def resume_thread(self, params: dict[str, object], timeout: float | None) -> dict[str, object]:
                self.calls.append(("resume", params, timeout))
                return {"thread": {"id": "stale-1", "status": {"type": "idle"}}}

        client = ThreadClient()
        args = SimpleNamespace(
            thread_id="stale-1",
            cwd=".",
            sandbox="read-only",
            model="gpt-5",
            personality="pragmatic",
            instructions="dev",
            ephemeral=False,
            print_thread_id=False,
        )

        thread_id, reused = await ensure_thread(client, args, "C:/repo", 1, 1, force_new=True)
        self.assertEqual((thread_id, reused), ("fresh-1", False))
        self.assertEqual(client.calls[0][0], "start")
        self.assertEqual(client.calls[0][2], 1)

    async def test_interleaved_notifications_are_buffered_until_matching_response(self) -> None:
        ws = MockWebSocket([])
        client = ProtocolClient(ws)

        async def recv() -> str:
            if len(ws.sent) == 1:
                return json.dumps({"jsonrpc": "2.0", "method": "thread/status/changed", "params": {"threadId": "t"}})
            return json.dumps(response_for(ws.sent[-1], {"ok": True}))

        calls = 0

        async def recv_sequence() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                return json.dumps({"jsonrpc": "2.0", "method": "thread/status/changed", "params": {"threadId": "t"}})
            return json.dumps(response_for(ws.sent[-1], {"ok": True}))

        ws.recv = recv_sequence  # type: ignore[method-assign]
        result = await client.request("x/test", {}, timeout=1)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(client.pending_messages[0]["method"], "thread/status/changed")

    async def test_rpc_failure_preserves_error_code(self) -> None:
        ws = MockWebSocket([])
        client = ProtocolClient(ws, retry=RetryConfig(attempts=1))

        async def recv() -> str:
            return json.dumps({"jsonrpc": "2.0", "id": ws.sent[-1]["id"], "error": {"code": -32602, "message": "bad params"}})

        ws.recv = recv  # type: ignore[method-assign]
        with self.assertRaises(RpcError) as ctx:
            await client.request("bad", {}, timeout=1)
        self.assertEqual(ctx.exception.code, -32602)
        self.assertEqual(ctx.exception.error_code, "invalid_params")

    async def test_wait_timeout_raises_timeout_error(self) -> None:
        ws = MockWebSocket([])
        client = ProtocolClient(ws)
        with self.assertRaises(asyncio.TimeoutError):
            await client.recv_json(0.01)

    async def test_turn_deadline_caps_total_wait(self) -> None:
        deadline = TurnDeadline(0.01)
        await asyncio.sleep(0.02)
        with self.assertRaises(asyncio.TimeoutError):
            deadline.remaining_timeout(10)

    async def test_turn_interrupt_request_can_be_sent(self) -> None:
        ws = MockWebSocket([])
        client = ProtocolClient(ws)

        async def recv() -> str:
            return json.dumps(response_for(ws.sent[-1], {}))

        ws.recv = recv  # type: ignore[method-assign]
        await client.request("turn/interrupt", {"threadId": "t", "turnId": "u"}, timeout=1, retry_overload=False)
        self.assertEqual(ws.sent[0]["method"], "turn/interrupt")

    async def test_thread_unsubscribe_request_can_be_sent(self) -> None:
        ws = MockWebSocket([])
        client = ProtocolClient(ws)

        async def recv() -> str:
            return json.dumps(response_for(ws.sent[-1], {"status": "unsubscribed"}))

        ws.recv = recv  # type: ignore[method-assign]
        result = await client.unsubscribe_thread("thread-1", timeout=1)
        self.assertEqual(result, {"status": "unsubscribed"})
        self.assertEqual(ws.sent[0]["method"], "thread/unsubscribe")
        self.assertEqual(ws.sent[0]["params"], {"threadId": "thread-1"})

    async def test_turn_start_omits_cwd_when_not_provided(self) -> None:
        ws = MockWebSocket([])
        client = ProtocolClient(ws)
        args = SimpleNamespace(no_stream=True, json=False, output_schema="", summary=False, out="")
        calls = 0

        async def recv() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                return json.dumps(response_for(ws.sent[-1], {"turn": {"id": "turn-1"}}))
            return json.dumps({"jsonrpc": "2.0", "method": "turn/completed", "params": {"turn": {"id": "turn-1", "status": "completed"}}})

        ws.recv = recv  # type: ignore[method-assign]
        exit_code, _ = await run_turn(client, args, "thread-1", None, "prompt", 1, None)
        self.assertEqual(exit_code, EXIT_SUCCESS)
        self.assertEqual(ws.sent[0]["method"], "turn/start")
        self.assertNotIn("cwd", ws.sent[0]["params"])

    async def test_detached_turn_starts_then_unsubscribes(self) -> None:
        ws = MockWebSocket([])
        client = ProtocolClient(ws)
        args = SimpleNamespace(output_schema="")

        async def recv() -> str:
            if ws.sent[-1]["method"] == "turn/start":
                return json.dumps(response_for(ws.sent[-1], {"turn": {"id": "turn-1", "status": "inProgress", "items": []}}))
            return json.dumps(response_for(ws.sent[-1], {"status": "unsubscribed"}))

        ws.recv = recv  # type: ignore[method-assign]
        result = await run_detached_turn(client, args, "thread-1", None, "prompt", 1)
        self.assertEqual(
            result,
            {
                "thread_id": "thread-1",
                "turn_id": "turn-1",
                "status": "detached",
                "turn_status": "inProgress",
                "unsubscribe_status": "unsubscribed",
            },
        )
        self.assertEqual([sent["method"] for sent in ws.sent], ["turn/start", "thread/unsubscribe"])
        self.assertNotIn("cwd", ws.sent[0]["params"])

    async def test_cancel_requests_interrupt_on_next_loop_iteration(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object]]] = []
                self.recv_calls = 0

            async def request(self, method: str, params: dict[str, object], timeout: float | None = None, deadline: object = None) -> dict[str, object]:
                self.calls.append((method, params))
                if method == "turn/start":
                    return {"turn": {"id": "turn-1"}}
                raise AssertionError(f"unexpected request: {method}")

            async def interrupt_turn(self, thread_id: str, turn_id: str, timeout: float | None = None) -> dict[str, object]:
                self.calls.append(("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}))
                return {}

            async def recv_json(self, timeout: float | None, deadline: object = None) -> dict[str, object]:
                self.recv_calls += 1
                self.calls.append(("recv_json", {"count": self.recv_calls}))
                if self.recv_calls == 1:
                    _cancel.active_turn_id = "turn-1"
                    _cancel.cancel_requested = True
                    return {"jsonrpc": "2.0", "method": "item/agentMessage/delta", "params": {"turnId": "turn-1", "delta": "hello"}}
                return {"jsonrpc": "2.0", "method": "turn/completed", "params": {"turn": {"id": "turn-1", "status": "completed"}}}

        client = FakeClient()
        args = SimpleNamespace(no_stream=True, json=False, output_schema="", summary=False, out="")
        original_cancel_state = (_cancel.active_turn_id, _cancel.cancel_requested)
        try:
            _cancel.active_turn_id = ""
            _cancel.cancel_requested = False
            exit_code, _ = await run_turn(client, args, "thread-1", None, "prompt", 1, None)
        finally:
            _cancel.active_turn_id, _cancel.cancel_requested = original_cancel_state

        self.assertEqual(exit_code, EXIT_SUCCESS)
        self.assertEqual([call[0] for call in client.calls], ["turn/start", "recv_json", "turn/interrupt", "recv_json"])

    async def test_repl_turn_uses_configured_deadline(self) -> None:
        ws = MockWebSocket([])
        client = ProtocolClient(ws)
        args = SimpleNamespace(
            json=False,
            no_stream=True,
            turn_deadline=2.5,
            thread_id="",
            instructions="dev",
        )
        seen_deadlines: list[float | None] = []

        async def fake_run_turn(*call_args: object, **_: object) -> tuple[int, dict[str, object] | None]:
            seen_deadlines.append(call_args[6])
            return EXIT_SUCCESS, None

        with mock.patch("builtins.input", side_effect=["hello", "/exit"]), mock.patch(
            "codex_ws_client.run_turn",
            side_effect=fake_run_turn,
        ):
            result = await run_repl(client, args, "thread-1", "C:/repo", 10, 20, 2.5)

        self.assertEqual(result, EXIT_SUCCESS)
        self.assertEqual(seen_deadlines, [2.5])

    def test_cli_help_smoke(self) -> None:
        script = SCRIPT_DIR / "codex_ws_client.py"
        proc = subprocess.run([sys.executable, str(script), "--help"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("Codex app-server WebSocket client", proc.stdout)

    def test_main_returns_sigint_for_forced_loop_stop_only(self) -> None:
        def fake_asyncio_run(coro: object) -> None:
            if hasattr(coro, "close"):
                coro.close()  # type: ignore[call-arg]
            raise RuntimeError("Event loop stopped before Future completed.")

        with mock.patch("codex_ws_client.parse_args", return_value=SimpleNamespace()), mock.patch(
            "codex_ws_client.asyncio.run",
            side_effect=fake_asyncio_run,
        ):
            from codex_ws_client import main

            self.assertEqual(main(), EXIT_SIGINT)

    def test_main_propagates_unexpected_runtime_error(self) -> None:
        def fake_asyncio_run(coro: object) -> None:
            if hasattr(coro, "close"):
                coro.close()  # type: ignore[call-arg]
            raise RuntimeError("unexpected runtime failure")

        with mock.patch("codex_ws_client.parse_args", return_value=SimpleNamespace()), mock.patch(
            "codex_ws_client.asyncio.run",
            side_effect=fake_asyncio_run,
        ):
            from codex_ws_client import main

            with self.assertRaises(RuntimeError) as ctx:
                main()
            self.assertEqual(str(ctx.exception), "unexpected runtime failure")

    async def test_overload_retries_with_backoff(self) -> None:
        ws = MockWebSocket([])
        client = ProtocolClient(ws, retry=RetryConfig(attempts=2, base_delay=0, jitter=0))
        calls = 0

        async def recv() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                return json.dumps({"jsonrpc": "2.0", "id": ws.sent[-1]["id"], "error": {"code": APP_SERVER_OVERLOADED, "message": "busy"}})
            return json.dumps(response_for(ws.sent[-1], {"ok": True}))

        ws.recv = recv  # type: ignore[method-assign]
        result = await client.request("maybeBusy", {}, timeout=1)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(ws.sent), 2)

    async def test_reconnect_case_uses_fresh_client_pending_queue(self) -> None:
        first = ProtocolClient(MockWebSocket([]))
        first.pending_messages.append({"jsonrpc": "2.0", "method": "old", "params": {}})
        second_ws = MockWebSocket([])
        second = ProtocolClient(second_ws)

        async def recv() -> str:
            return json.dumps(response_for(second_ws.sent[-1], {"ok": True}))

        second_ws.recv = recv  # type: ignore[method-assign]
        result = await second.request("afterReconnect", {}, timeout=1)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(second.pending_messages), 0)

    async def test_thread_list_uses_schema_params_and_updated_at_filter(self) -> None:
        ws = MockWebSocket([])
        client = ProtocolClient(ws)

        async def recv() -> str:
            return json.dumps(
                response_for(
                    ws.sent[-1],
                    {
                        "data": [
                            {"id": "old", "updatedAt": 100, "cwd": "C:/repo", "name": "alpha"},
                            {"id": "keep", "updatedAt": 200, "cwd": "C:/repo", "name": "alpha bravo"},
                            {"id": "new", "updatedAt": 300, "cwd": "C:/repo", "name": "alpha charlie"},
                        ]
                    },
                )
            )

        ws.recv = recv  # type: ignore[method-assign]
        result = await client.list_threads(1, cwd="C:/repo", title="alpha", updated_after="150", updated_before="250")
        self.assertEqual(ws.sent[0]["method"], "thread/list")
        self.assertEqual(ws.sent[0]["params"]["cwd"], "C:/repo")
        self.assertEqual(ws.sent[0]["params"]["searchTerm"], "alpha")
        self.assertEqual(ws.sent[0]["params"]["sortKey"], "updated_at")
        self.assertEqual([thread["id"] for thread in result["data"]], ["keep"])

    async def test_read_thread_uses_include_turns(self) -> None:
        ws = MockWebSocket([])
        client = ProtocolClient(ws)

        async def recv() -> str:
            return json.dumps(response_for(ws.sent[-1], {"thread": {"id": "thread-1", "status": {"type": "idle"}}}))

        ws.recv = recv  # type: ignore[method-assign]
        result = await client.read_thread("thread-1", 1, include_turns=True)
        self.assertEqual(ws.sent[0]["method"], "thread/read")
        self.assertTrue(ws.sent[0]["params"]["includeTurns"])
        self.assertEqual(result["thread"]["id"], "thread-1")

    def test_generated_schema_manifest_matches_installed_codex(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            schema_dir = Path(tmpdir)
            subprocess.run(["cmd", "/c", "codex", "app-server", "generate-json-schema", "--out", str(schema_dir)], check=True)
            for rel_path, expected_hash in SCHEMA_MANIFEST.items():
                self.assertEqual(_hash_schema(schema_dir / rel_path), expected_hash)

    def test_schema_payloads_validate_against_installed_codex_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            schema_dir = Path(tmpdir)
            subprocess.run(["cmd", "/c", "codex", "app-server", "generate-json-schema", "--out", str(schema_dir)], check=True)
            with (schema_dir / "v2" / "ThreadListParams.json").open("r", encoding="utf-8") as fh:
                thread_list_schema = json.load(fh)
            with (schema_dir / "v2" / "ThreadUnsubscribeParams.json").open("r", encoding="utf-8") as fh:
                thread_unsubscribe_schema = json.load(fh)
            with (schema_dir / "v2" / "TurnStartParams.json").open("r", encoding="utf-8") as fh:
                turn_start_schema = json.load(fh)
            validate(
                instance={"cwd": "C:/repo", "searchTerm": "alpha", "sortKey": "updated_at", "sortDirection": "desc"},
                schema=thread_list_schema,
            )
            validate(
                instance={"threadId": "thread-1", "cwd": "C:/repo", "approvalPolicy": "never", "input": [{"type": "text", "text": "prompt"}]},
                schema=turn_start_schema,
            )
            validate(
                instance={"threadId": "thread-1", "approvalPolicy": "never", "input": [{"type": "text", "text": "prompt"}]},
                schema=turn_start_schema,
            )
            validate(
                instance={"threadId": "thread-1"},
                schema=thread_unsubscribe_schema,
            )


if __name__ == "__main__":
    unittest.main()
