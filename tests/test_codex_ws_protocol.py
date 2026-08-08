from __future__ import annotations

import asyncio
from contextlib import redirect_stderr
import io
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
    is_terminal_turn_status,
    make_turn_params,
    run_detached_turn,
    run_thread_unload,
    run_turn,
    run_repl,
    run_client,
    sandbox_validation_error,
    wait_for_turn_terminal,
    resolve_default_model,
    normalize_protocol_cwd,
    parse_args,
    parse_headers,
    turn_metrics,
)

SCHEMA_MANIFEST = {
  "ClientNotification.json": "4446a1ae8626aa55d812836bfc2dae24213500d87c4759af2698f6199ea5b59f",
  "ClientRequest.json": "6a13d5ad2c85e61e12a7b97c8b447b37734601f63c55aa8ed70928ab30a71eb3",
  "JSONRPCError.json": "8835db6c4ada12ec628613fe2f571edc2c8fb55fc31bac706085e25ad1a08c0e",
  "ServerNotification.json": "023badaf3f0802767dd5e6029ac24bbbc90019f4853a77af5cb6e5a0cd911bcb",
  "ServerRequest.json": "5b31783aed46cdcdced357328fb6778d9a88c04f67e097eff79b4026343699f1",
  "v2/ThreadListParams.json": "693ac88dd3c3c163b422aae3a57aef5c06c65240cc2c2011813d71db9bfc4867",
  "v2/ThreadListResponse.json": "4db9a8ff72a36d6516c85cd62576b6925528d24b214fa6beef568e58b05769e9",
  "v2/ThreadReadParams.json": "ab07c67662e3a8db06a9a2905af350919c7f9a7d1d1c4055a33c26234b6c9f23",
  "v2/ThreadReadResponse.json": "721ef67972be98db6b45828aba6a7f006878909f0bdcd1af01dcc94643905e1e",
  "v2/ThreadResumeParams.json": "a238b91bfe282f51350202fd2f5e59d2219108dbf432a264f52decf7dbd7b42f",
  "v2/ThreadResumeResponse.json": "eebe7ad3292190e3150b266d36a938cbb2fe27a2f7d29b14ccd842f3e5197e2f",
  "v2/ThreadStartParams.json": "278d25b0c1771eafc2e0bdaac84c5bbc2cfbd30cc4b1feb15511a74385b0ec95",
  "v2/ThreadStartResponse.json": "60e16a7cdf72e2055782df0b07bc3a4715e502f0e2233d1eafe8fd534c329fed",
  "v2/TurnStartParams.json": "bf86fcd73b3282f99db13a402d2c661297849df8c93b7fbc54b234e3f7b1df94",
  "v2/TurnStartResponse.json": "c221fa3037fb3f1ec0250789b4042210bce37eaf7a17a92113e5b40917eb8840",
  "v2/TurnSteerParams.json": "18daa1fcfb60873beb1e1b132e142acf9812b21d69c8ef7ea7803bb748f10df7",
  "v2/TurnSteerResponse.json": "c0cee0b0c57af980fe2727679fbf2d3110ba28e8c9328f40d402fcc44ecb87bf",
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
    async def test_turn_json_exposes_actual_model_cache_usage_and_idle_duration(self) -> None:
        ws = MockWebSocket([])
        client = ProtocolClient(ws)
        args = SimpleNamespace(
            no_stream=True,
            json=True,
            output_schema="",
            summary=False,
            out="",
            effective_sandbox="read-only",
            resume_idle_duration_seconds=42.5,
        )
        calls = 0

        async def recv() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                return json.dumps(response_for(ws.sent[-1], {"turn": {"id": "turn-1", "model": "requested-model"}}))
            if calls == 2:
                return json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "thread/tokenUsage/updated",
                        "params": {
                            "threadId": "thread-1",
                            "lastTokenUsage": {
                                "input_tokens": 100,
                                "output_tokens": 20,
                                "input_tokens_details": {"cached_tokens": 70, "cache_write_tokens": 15},
                            },
                        },
                    }
                )
            if calls == 3:
                return json.dumps(
                    {"jsonrpc": "2.0", "method": "model/rerouted", "params": {"turnId": "turn-1", "toModel": "actual-model"}}
                )
            return json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "turn/completed",
                    "params": {"turn": {"id": "turn-1", "status": "completed"}},
                }
            )

        ws.recv = recv  # type: ignore[method-assign]
        exit_code, result = await run_turn(client, args, "thread-1", None, "prompt", 1, None)
        self.assertEqual(exit_code, EXIT_SUCCESS)
        self.assertIsNotNone(result)
        self.assertEqual(result["metrics"]["model"], "actual-model")
        self.assertEqual(result["metrics"]["cached_tokens"], 70)
        self.assertEqual(result["metrics"]["cache_write_tokens"], 15)
        self.assertEqual(result["metrics"]["idle_duration_seconds"], 42.5)

    def test_turn_metrics_accepts_responses_usage_shape(self) -> None:
        metrics = turn_metrics(
            {"model": "gpt-5.6", "usage": {"input_tokens": 10, "output_tokens": 2, "input_tokens_details": {"cached_tokens": 8, "cache_write_tokens": 1}}}
        )
        self.assertEqual(metrics, {"model": "gpt-5.6", "input_tokens": 10, "output_tokens": 2, "cached_tokens": 8, "cache_write_tokens": 1})

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
            resume_idle_duration_seconds=12.5,
        )

        async def recv() -> str:
            return json.dumps(
                response_for(
                    ws.sent[-1],
                    {"thread": {"id": "thread-1", "sandbox": "workspace-write", "updatedAt": "2020-01-01T00:00:00Z", "status": {"type": "idle"}}},
                )
            )

        ws.recv = recv  # type: ignore[method-assign]
        thread_id, reused = await ensure_thread(client, args, "C:/repo", 1, 1)
        self.assertEqual((thread_id, reused), ("thread-1", True))
        self.assertEqual(ws.sent[0]["method"], "thread/resume")
        self.assertEqual(ws.sent[0]["params"]["cwd"], "C:/repo")
        self.assertNotIn("sandbox", ws.sent[0]["params"])
        self.assertEqual(args.effective_sandbox, "workspace-write")
        self.assertEqual(args.resume_idle_duration_seconds, 12.5)

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
            return json.dumps(
                response_for(
                    ws.sent[-1],
                    {"thread": {"id": "thread-1", "sandbox": "workspace-write", "status": {"type": "idle"}}},
                )
            )

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
        self.assertEqual(client.calls[0][1]["sandbox"], "read-only")
        self.assertEqual(args.effective_sandbox, "read-only")

    async def test_resume_ttl_starts_fresh_thread_when_thread_is_idle_too_long(self) -> None:
        class ThreadClient:
            async def read_thread(self, thread_id: str, timeout: float | None, *, include_turns: bool = False) -> dict[str, object]:
                self.read_args = (thread_id, timeout, include_turns)
                return {"thread": {"id": thread_id, "updatedAt": "2020-01-01T00:00:00Z"}}

            async def start_thread(self, params: dict[str, object], timeout: float | None) -> dict[str, object]:
                self.start_args = (params, timeout)
                return {"thread": {"id": "fresh-ttl"}}

            async def resume_thread(self, params: dict[str, object], timeout: float | None) -> dict[str, object]:
                raise AssertionError("expired thread must not be resumed")

        client = ThreadClient()
        args = SimpleNamespace(
            thread_id="old-thread",
            cwd=".",
            sandbox="read-only",
            permissions="",
            model="gpt-5",
            personality="pragmatic",
            instructions="dev",
            ephemeral=False,
            print_thread_id=False,
            resume_ttl=1,
        )
        thread_id, reused = await ensure_thread(client, args, "C:/repo", 1, 1)
        self.assertEqual((thread_id, reused), ("fresh-ttl", False))
        self.assertEqual(args.rotation, {"decision": "fresh_thread", "reason": "resume_ttl_exceeded"})
        self.assertGreater(args.resume_idle_duration_seconds, 1)

    async def test_resume_ttl_fails_closed_to_fresh_thread_when_idle_timestamp_missing(self) -> None:
        class ThreadClient:
            async def read_thread(self, thread_id: str, timeout: float | None, *, include_turns: bool = False) -> dict[str, object]:
                return {"thread": {"id": thread_id}}

            async def start_thread(self, params: dict[str, object], timeout: float | None) -> dict[str, object]:
                return {"thread": {"id": "fresh-unknown-age"}}

            async def resume_thread(self, params: dict[str, object], timeout: float | None) -> dict[str, object]:
                raise AssertionError("TTL-unavailable thread must not be resumed")

        args = SimpleNamespace(
            thread_id="unknown-age",
            cwd=".",
            sandbox="read-only",
            permissions="",
            model="gpt-5",
            personality="pragmatic",
            instructions="dev",
            ephemeral=False,
            print_thread_id=False,
            resume_ttl=60,
        )
        thread_id, reused = await ensure_thread(ThreadClient(), args, "C:/repo", 1, 1)
        self.assertEqual((thread_id, reused), ("fresh-unknown-age", False))
        self.assertEqual(args.rotation, {"decision": "fresh_thread", "reason": "resume_ttl_unavailable"})

    async def test_new_thread_can_select_a_named_permission_profile(self) -> None:
        class ThreadClient:
            def __init__(self) -> None:
                self.params: dict[str, object] = {}

            async def start_thread(self, params: dict[str, object], timeout: float | None) -> dict[str, object]:
                self.params = params
                return {"thread": {"id": "profile-thread"}}

        client = ThreadClient()
        args = SimpleNamespace(
            thread_id="",
            cwd=".",
            sandbox=None,
            permissions="oa-review-output",
            model="gpt-5",
            personality="pragmatic",
            instructions="review",
            ephemeral=False,
            print_thread_id=False,
            runtime_workspace_root=["C:/review-output"],
        )

        thread_id, reused = await ensure_thread(client, args, "C:/review-output", 1, 1)

        self.assertEqual((thread_id, reused), ("profile-thread", False))
        self.assertEqual(client.params["permissions"], "oa-review-output")
        self.assertEqual(client.params["runtimeWorkspaceRoots"], ["C:/review-output"])
        self.assertNotIn("sandbox", client.params)
        self.assertEqual(args.effective_sandbox, "oa-review-output")

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

    async def test_turn_steer_uses_the_expected_active_turn_without_resuming(self) -> None:
        ws = MockWebSocket([])
        client = ProtocolClient(ws)

        async def recv() -> str:
            return json.dumps(response_for(ws.sent[-1], {"turnId": "turn-1"}))

        ws.recv = recv  # type: ignore[method-assign]
        result = await client.steer_turn("thread-1", "turn-1", "Correct the last instruction.", timeout=1)
        self.assertEqual(result, {"turnId": "turn-1"})
        self.assertEqual(ws.sent[0]["method"], "turn/steer")
        self.assertEqual(
            ws.sent[0]["params"],
            {
                "threadId": "thread-1",
                "expectedTurnId": "turn-1",
                "input": [{"type": "text", "text": "Correct the last instruction."}],
            },
        )

    async def test_wait_for_turn_terminal_polls_persisted_state_without_resuming(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self.responses = [
                    {"thread": {"id": "thread-1", "turns": [{"id": "turn-1", "status": "inProgress"}]}},
                    {"thread": {"id": "thread-1", "turns": [{"id": "turn-1", "status": "completed", "items": [{"type": "agentMessage", "text": "done"}]}]}},
                ]

            async def read_thread(
                self, thread_id: str, timeout: float | None, *, include_turns: bool, deadline: object = None
            ) -> dict[str, object]:
                self.calls.append({"threadId": thread_id, "includeTurns": include_turns})
                return self.responses.pop(0)

        client = FakeClient()
        code, result = await wait_for_turn_terminal(client, "thread-1", "turn-1", timeout=1, wait_timeout=1, poll_interval=0)
        self.assertEqual(code, EXIT_SUCCESS)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["text"], "done")
        self.assertEqual(result["wait_status"], "terminal")
        self.assertEqual(result["poll_count"], 2)
        self.assertEqual(client.calls, [{"threadId": "thread-1", "includeTurns": True}, {"threadId": "thread-1", "includeTurns": True}])

    def test_terminal_turn_status_matches_app_server_values(self) -> None:
        self.assertTrue(is_terminal_turn_status("completed"))
        self.assertTrue(is_terminal_turn_status("interrupted"))
        self.assertTrue(is_terminal_turn_status("failed"))
        self.assertFalse(is_terminal_turn_status("inProgress"))

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

    async def test_thread_archive_captures_notification_arriving_before_rpc_response(self) -> None:
        ws = MockWebSocket([])
        client = ProtocolClient(ws)
        received = 0

        async def recv() -> str:
            nonlocal received
            received += 1
            if received == 1:
                return json.dumps({"jsonrpc": "2.0", "method": "thread/archived", "params": {"threadId": "thread-1"}})
            return json.dumps(response_for(ws.sent[-1], {"status": "archived"}))

        ws.recv = recv  # type: ignore[method-assign]
        result = await client.archive_thread("thread-1", timeout=1)
        self.assertEqual(ws.sent[0]["method"], "thread/archive")
        self.assertEqual(ws.sent[0]["params"], {"threadId": "thread-1"})
        self.assertEqual(result["archive_result"], {"status": "archived"})
        self.assertEqual(result["archived_notification"]["method"], "thread/archived")
        self.assertEqual(result["archived_notification"]["params"], {"threadId": "thread-1"})

    async def test_thread_archive_waits_for_notification_after_rpc_response(self) -> None:
        ws = MockWebSocket([])
        client = ProtocolClient(ws)
        received = 0

        async def recv() -> str:
            nonlocal received
            received += 1
            if received == 1:
                return json.dumps(response_for(ws.sent[-1], {"status": "archived"}))
            return json.dumps({"jsonrpc": "2.0", "method": "thread/archived", "params": {"threadId": "thread-1"}})

        ws.recv = recv  # type: ignore[method-assign]
        result = await client.archive_thread("thread-1", timeout=1)
        self.assertEqual(received, 2)
        self.assertEqual(result["archive_result"], {"status": "archived"})
        self.assertEqual(result["archived_notification"]["method"], "thread/archived")

    async def test_thread_archive_times_out_without_archived_notification(self) -> None:
        ws = MockWebSocket([])
        client = ProtocolClient(ws)
        received = 0

        async def recv() -> str:
            nonlocal received
            received += 1
            if received == 1:
                return json.dumps(response_for(ws.sent[-1], {"status": "archived"}))
            raise asyncio.TimeoutError()

        ws.recv = recv  # type: ignore[method-assign]
        with self.assertRaises(asyncio.TimeoutError):
            await client.archive_thread("thread-1", timeout=0.01)
        self.assertEqual(ws.sent[0]["method"], "thread/archive")

    async def test_thread_unarchive_and_delete_are_thin_rpc_wrappers(self) -> None:
        ws = MockWebSocket([])
        client = ProtocolClient(ws)

        async def recv() -> str:
            return json.dumps(response_for(ws.sent[-1], {"ok": True}))

        ws.recv = recv  # type: ignore[method-assign]
        self.assertEqual(await client.unarchive_thread("thread-1", timeout=1), {"ok": True})
        self.assertEqual(await client.delete_thread("thread-1", timeout=1), {"ok": True})
        self.assertEqual([sent["method"] for sent in ws.sent], ["thread/unarchive", "thread/delete"])
        self.assertEqual([sent["params"] for sent in ws.sent], [{"threadId": "thread-1"}, {"threadId": "thread-1"}])

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

    async def test_json_turn_result_includes_effective_sandbox(self) -> None:
        ws = MockWebSocket([])
        client = ProtocolClient(ws)
        args = SimpleNamespace(
            no_stream=True,
            json=True,
            output_schema="",
            summary=False,
            out="",
            effective_sandbox="workspace-write",
        )
        calls = 0

        async def recv() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                return json.dumps(response_for(ws.sent[-1], {"turn": {"id": "turn-1"}}))
            return json.dumps(
                {"jsonrpc": "2.0", "method": "turn/completed", "params": {"turn": {"id": "turn-1", "status": "completed"}}}
            )

        ws.recv = recv  # type: ignore[method-assign]
        exit_code, result = await run_turn(client, args, "thread-1", None, "prompt", 1, None)
        self.assertEqual(exit_code, EXIT_SUCCESS)
        self.assertIsNotNone(result)
        self.assertEqual(result["sandbox"], "workspace-write")

    async def test_detached_turn_starts_then_unsubscribes(self) -> None:
        ws = MockWebSocket([])
        client = ProtocolClient(ws)
        args = SimpleNamespace(output_schema="", effective_sandbox="danger-full-access")

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
                "sandbox": "danger-full-access",
            },
        )
        self.assertEqual([sent["method"] for sent in ws.sent], ["turn/start", "thread/unsubscribe"])
        self.assertNotIn("cwd", ws.sent[0]["params"])
        self.assertTrue(ws.closed)
        self.assertEqual(ws.close_calls, 1)

    async def test_detached_turn_emits_available_metrics_for_harvest(self) -> None:
        ws = MockWebSocket([])
        client = ProtocolClient(ws)
        args = SimpleNamespace(output_schema="", effective_sandbox="read-only")

        async def recv() -> str:
            if ws.sent[-1]["method"] == "turn/start":
                return json.dumps(
                    response_for(
                        ws.sent[-1],
                        {
                            "turn": {
                                "id": "turn-1",
                                "status": "inProgress",
                                "model": "gpt-5.6",
                                "usage": {
                                    "input_tokens": 100,
                                    "output_tokens": 5,
                                    "input_tokens_details": {"cached_tokens": 80, "cache_write_tokens": 10},
                                },
                            }
                        },
                    )
                )
            return json.dumps(response_for(ws.sent[-1], {"status": "unsubscribed"}))

        ws.recv = recv  # type: ignore[method-assign]
        result = await run_detached_turn(client, args, "thread-1", None, "prompt", 1)
        self.assertEqual(result["metrics"], {
            "model": "gpt-5.6",
            "input_tokens": 100,
            "output_tokens": 5,
            "cached_tokens": 80,
            "cache_write_tokens": 10,
        })

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

    def test_sandbox_argument_is_explicit_and_restricted(self) -> None:
        with mock.patch.object(sys, "argv", ["codex_ws_client.py", "--sandbox", "danger-full-access", "prompt"]):
            args = parse_args()
        self.assertEqual(args.sandbox, "danger-full-access")

        with mock.patch.object(sys, "argv", ["codex_ws_client.py", "prompt"]):
            args = parse_args()
        self.assertIsNone(args.sandbox)

    def test_named_permissions_argument_is_explicit(self) -> None:
        with mock.patch.object(
            sys,
            "argv",
            ["codex_ws_client.py", "--permissions", "oa-review-output", "prompt"],
        ):
            args = parse_args()
        self.assertEqual(args.permissions, "oa-review-output")
        self.assertIsNone(args.sandbox)

    def test_runtime_workspace_roots_are_explicit_and_repeatable(self) -> None:
        with mock.patch.object(
            sys,
            "argv",
            [
                "codex_ws_client.py",
                "--permissions",
                "oa-review-output",
                "--runtime-workspace-root",
                "C:/output-a",
                "--runtime-workspace-root",
                "C:/output-b",
                "prompt",
            ],
        ):
            args = parse_args()
        self.assertEqual(args.runtime_workspace_root, ["C:/output-a", "C:/output-b"])
        turn = make_turn_params(args, "thread-1", "C:/harness", "review")
        self.assertEqual(
            turn["runtimeWorkspaceRoots"], ["C:/output-a", "C:/output-b"]
        )
        self.assertEqual(turn["permissions"], "oa-review-output")

    def test_reasoning_effort_is_sent_as_turn_effort(self) -> None:
        with mock.patch.object(
            sys,
            "argv",
            ["codex_ws_client.py", "--permissions", "oa-review-output", "--effort", "high", "prompt"],
        ):
            args = parse_args()
        turn = make_turn_params(args, "thread-1", "C:/harness", "review")
        self.assertEqual(turn["effort"], "high")

    def test_reasoning_effort_is_omitted_by_default(self) -> None:
        with mock.patch.object(
            sys,
            "argv",
            ["codex_ws_client.py", "--permissions", "oa-review-output", "prompt"],
        ):
            args = parse_args()
        turn = make_turn_params(args, "thread-1", "C:/harness", "review")
        self.assertNotIn("effort", turn)

    def test_inspection_commands_are_exempt_from_sandbox_selection(self) -> None:
        with mock.patch.object(sys, "argv", ["codex_ws_client.py", "--list-threads"]):
            args = parse_args()
        self.assertIsNone(sandbox_validation_error(args, inspection_operation=True))

    async def test_new_prompt_thread_requires_exactly_one_permission_policy_before_connecting(self) -> None:
        with mock.patch.object(sys, "argv", ["codex_ws_client.py", "prompt"]):
            args = parse_args()
        stderr = io.StringIO()
        with redirect_stderr(stderr), mock.patch("codex_ws_client.websockets.connect") as connect:
            result = await run_client(args)
        self.assertEqual(result, 2)
        self.assertIn("exactly one of --sandbox or --permissions", stderr.getvalue())
        connect.assert_not_called()

    async def test_new_prompt_thread_rejects_sandbox_and_permissions_together_before_connecting(self) -> None:
        with mock.patch.object(
            sys,
            "argv",
            [
                "codex_ws_client.py",
                "--sandbox",
                "read-only",
                "--permissions",
                "oa-review-output",
                "prompt",
            ],
        ):
            args = parse_args()
        stderr = io.StringIO()
        with redirect_stderr(stderr), mock.patch("codex_ws_client.websockets.connect") as connect:
            result = await run_client(args)
        self.assertEqual(result, 2)
        self.assertIn("exactly one of --sandbox or --permissions", stderr.getvalue())
        connect.assert_not_called()

    async def test_resumed_prompt_rejects_sandbox_before_connecting(self) -> None:
        with mock.patch.object(
            sys,
            "argv",
            ["codex_ws_client.py", "--thread-id", "thread-1", "--sandbox", "workspace-write", "prompt"],
        ):
            args = parse_args()
        stderr = io.StringIO()
        with redirect_stderr(stderr), mock.patch("codex_ws_client.websockets.connect") as connect:
            result = await run_client(args)
        self.assertEqual(result, 2)
        self.assertIn("start a fresh thread", stderr.getvalue())
        connect.assert_not_called()

    def test_resumed_prompt_sends_named_permissions_on_turn_start(self) -> None:
        with mock.patch.object(
            sys,
            "argv",
            ["codex_ws_client.py", "--thread-id", "thread-1", "--permissions", "oa-review-output", "prompt"],
        ):
            args = parse_args()
        self.assertIsNone(sandbox_validation_error(args, inspection_operation=False))
        params = make_turn_params(args, "thread-1", "C:/harness", "review")
        self.assertEqual(params["permissions"], "oa-review-output")

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

        with mock.patch.object(
            sys,
            "argv",
            [
                "codex_ws_client.py",
                "--archive-thread",
                "thread-1",
                "--unarchive-thread",
                "thread-2",
                "--delete-thread",
                "thread-3",
            ],
        ):
            args = parse_args()
        self.assertEqual(args.archive_thread, "thread-1")
        self.assertEqual(args.unarchive_thread, "thread-2")
        self.assertEqual(args.delete_thread, "thread-3")

    def test_active_turn_cli_commands_parse_with_requested_names(self) -> None:
        with mock.patch.object(
            sys,
            "argv",
            [
                "codex_ws_client.py",
                "--steer-turn",
                "thread-1",
                "turn-1",
                "Use the corrected scope.",
                "--wait-turn-timeout",
                "45",
                "--wait-turn-poll-interval",
                "2",
            ],
        ):
            args = parse_args()
        self.assertEqual(args.steer_turn, ["thread-1", "turn-1", "Use the corrected scope."])
        self.assertIsNone(args.wait_turn)
        self.assertEqual(args.wait_turn_timeout, 45)
        self.assertEqual(args.wait_turn_poll_interval, 2)
        with mock.patch.object(sys, "argv", ["codex_ws_client.py", "--wait-turn", "thread-1", "turn-1"]):
            args = parse_args()
        self.assertEqual(args.wait_turn, ["thread-1", "turn-1"])

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

    def test_normalize_protocol_cwd_preserves_remote_posix_path_on_windows(self) -> None:
        with mock.patch.object(os, "name", "nt"):
            self.assertEqual(
                normalize_protocol_cwd("/home/ec2-user/openarchitect/workspace"),
                "/home/ec2-user/openarchitect/workspace",
            )

    def test_normalize_protocol_cwd_resolves_local_path(self) -> None:
        self.assertEqual(normalize_protocol_cwd("."), str(Path(".").resolve()))

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
                            "model": "gpt-5.6",
                            "usage": {
                                "input_tokens": 12,
                                "output_tokens": 3,
                                "input_tokens_details": {"cached_tokens": 8, "cache_write_tokens": 1},
                            },
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
        self.assertEqual(result["metrics"]["model"], "gpt-5.6")
        self.assertEqual(result["metrics"]["cached_tokens"], 8)
        self.assertEqual(result["metrics"]["cache_write_tokens"], 1)

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
            with (schema_dir / "v2" / "TurnSteerParams.json").open("r", encoding="utf-8") as fh:
                turn_steer_schema = json.load(fh)
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
                instance={
                    "threadId": "thread-1",
                    "expectedTurnId": "turn-1",
                    "input": [{"type": "text", "text": "correct the active turn"}],
                },
                schema=turn_steer_schema,
            )
            validate(
                instance={"threadId": "thread-1"},
                schema=thread_unsubscribe_schema,
            )


if __name__ == "__main__":
    unittest.main()
