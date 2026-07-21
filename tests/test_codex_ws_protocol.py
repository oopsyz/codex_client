from __future__ import annotations

import asyncio
import hashlib
import json
import os
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
    extract_turn,
    active_turn_ids,
    run_detached_turn,
    run_thread_unload,
    run_turn,
    run_repl,
    resolve_default_model,
    parse_args,
    parse_headers,
)

SCHEMA_MANIFEST = {
  "ClientNotification.json": "4446a1ae8626aa55d812836bfc2dae24213500d87c4759af2698f6199ea5b59f",
  "ClientRequest.json": "5be9bf8117c33b8d1c20a25cd72eaea9122899cd527744ffaac1d12e3f0ba0fb",
  "JSONRPCError.json": "8835db6c4ada12ec628613fe2f571edc2c8fb55fc31bac706085e25ad1a08c0e",
  "ServerNotification.json": "f81a1c37bf45dad5beeede09f096b6051a5610979342fbc08b38561c1264b6be",
  "ServerRequest.json": "356862470133e0179625d46b6060f4db195b14ed9e2e5804d6402658791ed791",
  "v2/ThreadListParams.json": "59eb776ac4f7405f6dd45c953f62da0e8b94b4433aaab79936472471982fe6dd",
  "v2/ThreadListResponse.json": "f1023c4753f7c8471dd8ac0d75f5867ef6e64294d4119e50c6c0b2793abca4b5",
  "v2/ThreadReadParams.json": "ab07c67662e3a8db06a9a2905af350919c7f9a7d1d1c4055a33c26234b6c9f23",
  "v2/ThreadReadResponse.json": "72eda7e776a6a30f52bc161cd25703e1fd4fb60ed92969b9ddb3978bbae4ae21",
  "v2/ThreadResumeParams.json": "d867460ca8c348a8652ea0578ccaa4d67568c2bb462c1c71cd13ac4dcf91dc1b",
  "v2/ThreadResumeResponse.json": "87a2044caee7ac69a00cf159bf83a0da15a851222761113301f082e627cd6000",
  "v2/ThreadStartParams.json": "d960450fe2d0c1bf65f5aad42b070faefd59973ea9645d021b011ebdf23b5c03",
  "v2/ThreadStartResponse.json": "185c3a50c61ae83bc8ac9556e06c2bb759be5a7182017e1121d5992d7c2d2eca",
  "v2/TurnStartParams.json": "51c6606fb4b7c4efb8fb102afebcc30cc2ee8fe37bdb9da87be9a16880c2fc0a",
  "v2/TurnStartResponse.json": "d0fdbfb0058b2f964b17d770fa476819c4a2f574c552e703982c3568c016179f",
}


class HeaderInputTests(unittest.TestCase):
    def test_header_env_reads_secret_without_requiring_it_in_argv(self) -> None:
        headers = parse_headers(
            [],
            ["Authorization=CODEX_TEST_AUTH"],
            environ={"CODEX_TEST_AUTH": "Bearer secret-value"},
        )
        self.assertEqual(headers, {"Authorization": "Bearer secret-value"})

    def test_header_env_missing_variable_names_only_the_variable(self) -> None:
        with self.assertRaisesRegex(ValueError, "CODEX_TEST_MISSING") as raised:
            parse_headers([], ["Authorization=CODEX_TEST_MISSING"], environ={})
        self.assertNotIn("Authorization: Bearer", str(raised.exception))


def _hash_schema(path: Path) -> str:
    normalized = json.dumps(json.loads(path.read_text(encoding="utf-8")), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class MockWebSocket:
    def __init__(self, messages: list[dict[str, object] | str]) -> None:
        self.messages = asyncio.Queue()
        for message in messages:
            self.messages.put_nowait(message if isinstance(message, str) else json.dumps(message))
        self.sent: list[dict[str, object]] = []
        self.closed = False
        self.close_calls = 0

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def recv(self) -> str:
        item = await self.messages.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True
        self.close_calls += 1


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

    async def test_background_terminals_clean_request_can_be_sent(self) -> None:
        ws = MockWebSocket([])
        client = ProtocolClient(ws)

        async def recv() -> str:
            return json.dumps(response_for(ws.sent[-1], {}))

        ws.recv = recv  # type: ignore[method-assign]
        result = await client.clean_background_terminals("thread-1", timeout=1)
        self.assertEqual(result, {})
        self.assertEqual(ws.sent[0]["method"], "thread/backgroundTerminals/clean")
        self.assertEqual(ws.sent[0]["params"], {"threadId": "thread-1"})

    async def test_background_terminal_terminate_request_uses_app_server_process_id(self) -> None:
        ws = MockWebSocket([])
        client = ProtocolClient(ws)

        async def recv() -> str:
            return json.dumps(response_for(ws.sent[-1], {"terminated": True}))

        ws.recv = recv  # type: ignore[method-assign]
        result = await client.terminate_background_terminal("thread-1", "42", timeout=1)
        self.assertEqual(result, {"terminated": True})
        self.assertEqual(ws.sent[0]["method"], "thread/backgroundTerminals/terminate")
        self.assertEqual(ws.sent[0]["params"], {"threadId": "thread-1", "processId": "42"})

    async def test_unload_thread_interrupts_then_cleans_unsubscribes_and_waits_for_close(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls: list[tuple[str, object]] = []
                self.notifications = [
                    {"method": "turn/completed", "params": {"turn": {"id": "turn-active", "status": "interrupted"}}},
                    {"method": "thread/closed", "params": {"threadId": "thread-1"}},
                ]

            async def resume_thread(self, params: dict[str, object], timeout: float | None) -> dict[str, object]:
                self.calls.append(("thread/resume", params))
                return {"thread": {"id": "thread-1"}}

            async def read_thread(self, thread_id: str, timeout: float | None, *, include_turns: bool) -> dict[str, object]:
                self.calls.append(("thread/read", {"threadId": thread_id, "includeTurns": include_turns}))
                return {"thread": {"id": thread_id, "turns": [{"id": "turn-active", "status": "inProgress"}, {"id": "turn-done", "status": "completed"}]}}

            async def interrupt_turn(self, thread_id: str, turn_id: str, timeout: float | None = None) -> dict[str, object]:
                self.calls.append(("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}))
                return {}

            async def recv_json(self, timeout: float | None, deadline: object = None) -> dict[str, object]:
                self.calls.append(("recv_json", timeout))
                return self.notifications.pop(0)

            async def clean_background_terminals(self, thread_id: str, timeout: float | None = None) -> dict[str, object]:
                self.calls.append(("thread/backgroundTerminals/clean", {"threadId": thread_id}))
                return {}

            async def unsubscribe_thread(self, thread_id: str, timeout: float | None = None) -> dict[str, object]:
                self.calls.append(("thread/unsubscribe", {"threadId": thread_id}))
                return {"status": "unsubscribed"}

        client = FakeClient()
        result = await run_thread_unload(client, "thread-1", timeout=1, grace_period=1800)
        self.assertEqual(
            [call[0] for call in client.calls],
            ["thread/resume", "thread/read", "turn/interrupt", "recv_json", "thread/backgroundTerminals/clean", "thread/unsubscribe", "recv_json"],
        )
        self.assertEqual(result["interrupted_turn_ids"], ["turn-active"])
        self.assertEqual(result["unsubscribe_status"], "unsubscribed")
        self.assertEqual(result["unload_status"], "thread_closed")

    def test_active_turn_ids_only_includes_in_progress_turns(self) -> None:
        self.assertEqual(
            active_turn_ids({"thread": {"turns": [{"id": "active", "status": "inProgress"}, {"id": "finished", "status": "completed"}, {"id": "missing"}]}}),
            ["active"],
        )

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
        self.assertTrue(ws.closed)
        self.assertEqual(ws.close_calls, 1)

    async def test_turn_completion_closes_socket_on_terminal_response(self) -> None:
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
        self.assertTrue(ws.closed)
        self.assertEqual(ws.close_calls, 1)

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
        self.assertTrue(ws.closed)
        self.assertEqual(ws.close_calls, 1)

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

    def test_lifecycle_cli_commands_parse_with_requested_names(self) -> None:
        with mock.patch.object(sys, "argv", ["codex_ws_client.py", "--list-background-terminals", "thread-1"]):
            args = parse_args()
        self.assertEqual(args.background_terminals, "thread-1")
        with mock.patch.object(
            sys,
            "argv",
            [
                "codex_ws_client.py",
                "--clean-background-terminals",
                "thread-1",
                "--terminate-background-terminal",
                "thread-1",
                "42",
                "--unsubscribe-thread",
                "thread-1",
                "--list-loaded-threads",
            ],
        ):
            args = parse_args()
        self.assertEqual(args.clean_background_terminals, "thread-1")
        self.assertEqual(args.terminate_background_terminal, ["thread-1", "42"])
        self.assertEqual(args.unsubscribe_thread, "thread-1")
        self.assertTrue(args.list_loaded_threads)

    def test_resolve_default_model_reads_codex_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_dir = Path(tmp) / ".codex"
            codex_dir.mkdir()
            (codex_dir / "config.toml").write_text('model = "gpt-5.4-mini"\n', encoding="utf-8")
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_dir)}, clear=False):
                self.assertEqual(resolve_default_model(), "gpt-5.4-mini")

    def test_resolve_default_model_falls_back_when_config_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"CODEX_HOME": tmp}, clear=False):
                self.assertEqual(resolve_default_model(), "gpt-5")

    def test_resolve_default_model_prefers_project_config_over_user_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            subdir = root / "nested" / "work"
            subdir.mkdir(parents=True)
            (root / ".git").mkdir()
            user_codex = Path(tmp) / "user-codex"
            user_codex.mkdir()
            (user_codex / "config.toml").write_text('model = "gpt-5.4-mini"\n', encoding="utf-8")
            project_codex = root / ".codex"
            project_codex.mkdir()
            (project_codex / "config.toml").write_text('model = "gpt-5.5"\n', encoding="utf-8")
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(user_codex)}, clear=False):
                self.assertEqual(resolve_default_model(subdir), "gpt-5.5")

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

    async def test_thread_diagnostic_methods_send_pagination_and_detail_params(self) -> None:
        ws = MockWebSocket([])
        client = ProtocolClient(ws)

        async def recv() -> str:
            return json.dumps(response_for(ws.sent[-1], {"data": []}))

        ws.recv = recv  # type: ignore[method-assign]
        await client.list_loaded_threads(1)
        await client.list_thread_items("thread-1", 1, cursor="items-next", limit=7, turn_id="turn-1")
        await client.list_thread_turns("thread-1", 1, cursor="turns-next", limit=8, sort_direction="asc", items_view="full")
        await client.list_background_terminals("thread-1", 1, cursor="terminal-next", limit=9)
        await client.set_thread_name("thread-1", "work item", 1)

        self.assertEqual(ws.sent[0]["method"], "thread/loaded/list")
        self.assertEqual(ws.sent[1], {"jsonrpc": "2.0", "id": ws.sent[1]["id"], "method": "thread/items/list", "params": {"threadId": "thread-1", "limit": 7, "cursor": "items-next", "turnId": "turn-1"}})
        self.assertEqual(ws.sent[2]["params"]["itemsView"], "full")
        self.assertEqual(ws.sent[2]["params"]["sortDirection"], "asc")
        self.assertEqual(ws.sent[3]["params"]["cursor"], "terminal-next")
        self.assertEqual(ws.sent[4]["params"], {"threadId": "thread-1", "name": "work item"})

    async def test_thread_list_sends_all_supported_filters(self) -> None:
        ws = MockWebSocket([])
        client = ProtocolClient(ws)

        async def recv() -> str:
            return json.dumps(response_for(ws.sent[-1], {"data": [], "nextCursor": None}))

        ws.recv = recv  # type: ignore[method-assign]
        await client.list_threads(
            1,
            cursor="next",
            limit=12,
            sort_key="recency_at",
            sort_direction="asc",
            cwd=["C:/one", "C:/two"],
            title="bug",
            model_providers=["openai"],
            source_kinds=["cli", "subAgent"],
            archived=True,
            use_state_db_only=True,
            parent_thread_id="parent",
        )
        params = ws.sent[0]["params"]
        self.assertEqual(params["cursor"], "next")
        self.assertEqual(params["limit"], 12)
        self.assertEqual(params["sortKey"], "recency_at")
        self.assertEqual(params["cwd"], ["C:/one", "C:/two"])
        self.assertEqual(params["modelProviders"], ["openai"])
        self.assertEqual(params["sourceKinds"], ["cli", "subAgent"])
        self.assertTrue(params["archived"])
        self.assertTrue(params["useStateDbOnly"])
        self.assertEqual(params["parentThreadId"], "parent")

    def test_extract_turn_normalizes_agent_messages(self) -> None:
        result = extract_turn(
            {
                "thread": {
                    "id": "thread-1",
                    "turns": [
                        {
                            "id": "turn-1",
                            "status": "completed",
                            "items": [
                                {"type": "reasoning", "id": "reason-1"},
                                {"type": "agentMessage", "id": "msg-1", "text": "first"},
                                {"type": "agentMessage", "id": "msg-2", "text": " second"},
                            ],
                        }
                    ],
                }
            },
            "turn-1",
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["thread_id"], "thread-1")
        self.assertEqual(result["turn_id"], "turn-1")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["text"], "first second")

    def test_extract_turn_distinguishes_missing_turn(self) -> None:
        self.assertIsNone(extract_turn({"thread": {"id": "thread-1", "turns": []}}, "turn-missing"))

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
