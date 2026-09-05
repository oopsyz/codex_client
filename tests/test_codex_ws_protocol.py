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
from time import monotonic

from jsonschema import validate
from types import SimpleNamespace

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "skills" / "codex-ws-client" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from codex_ws_client import (  # noqa: E402
    APP_SERVER_OVERLOADED,
    BoundedAppServerClient,
    BoundedClientProfile,
    BoundedProtocolError,
    BoundedRequestResult,
    default_server_request_handler,
    EXIT_SUCCESS,
    EXIT_SIGINT,
    _cancel,
    ProtocolClient,
    NotificationObservation,
    RetryConfig,
    RpcError,
    TurnDeadline,
    ServerNotificationEnvelope,
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
    open_bounded_client,
    parse_server_notification_envelope,
    parse_args,
    parse_headers,
    turn_metrics,
)

SCHEMA_MANIFEST = {
  "ClientNotification.json": "4446a1ae8626aa55d812836bfc2dae24213500d87c4759af2698f6199ea5b59f",
  "ClientRequest.json": "c97c23877dd18a9fbc43a3bb87c72676acfbebdc14a2244342ee05dd95cfa38c",
  "JSONRPCError.json": "8835db6c4ada12ec628613fe2f571edc2c8fb55fc31bac706085e25ad1a08c0e",
  "ServerNotification.json": "4035ecb68922741c577bb6570208c144916d919a023316f7194c5d5d30d92039",
  "ServerRequest.json": "32f5e9fe877ff8d30a0255f7e68d63d6fcb616777a9fd0b0cf43f7b866c0627f",
  "v2/ThreadListParams.json": "693ac88dd3c3c163b422aae3a57aef5c06c65240cc2c2011813d71db9bfc4867",
  "v2/ThreadListResponse.json": "ca58e6edaded6165887236d1ef4ce35174e3315417327e5c52d6b27818f4edf2",
  "v2/ThreadReadParams.json": "034ae7f41fe195edb1010ab8927ac4215caacabbf24f15da96170f604aacb901",
  "v2/ThreadReadResponse.json": "8d079fb42a4faa554c39831e0a08a38cfecead5e0767a248f98c3b3640b6bae2",
  "v2/ThreadResumeParams.json": "537f95411388c2a674f9b74751e6ac268fd83253a9a654a41240cf1826584cdc",
  "v2/ThreadResumeResponse.json": "673ba797791fb67a4a5c6c69b4a98db1086ecc3c94f630c10c7a3839fa739ce7",
  "v2/ThreadStartParams.json": "278d25b0c1771eafc2e0bdaac84c5bbc2cfbd30cc4b1feb15511a74385b0ec95",
  "v2/ThreadStartResponse.json": "46011408b2ed3cf1f187425238adc037531f8b1de5bd09695428c39b546b9be7",
  "v2/TurnStartParams.json": "8a29a6fb75c063013567a01133fc4db4052771d22d3c88b634506f2a763df835",
  "v2/TurnStartResponse.json": "964fac59b8e94f4139d9091b6495da2e82a6cc1939f91c5669c8320fd53234d3",
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

    def fail_connection(self) -> None:
        self.closed = True


def response_for(sent: dict[str, object], result: dict[str, object]) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": sent["id"], "result": result}


class BoundedAdapterTests(unittest.IsolatedAsyncioTestCase):
    def _profile(self, validator=None, **kwargs):
        return BoundedClientProfile(
            "ws://127.0.0.1:8765",
            notification_validator=validator,
            **kwargs,
        )

    async def test_current_flattened_notification_is_admitted_without_retaining_raw_params(self) -> None:
        ws = MockWebSocket(
            [
                {"method": "remoteControl/status/changed", "params": {"status": "connected"}, "emittedAtMs": 1234},
                {"id": 17, "result": {"ok": True}},
            ]
        )
        seen: list[ServerNotificationEnvelope] = []

        def validate_notification(envelope):
            seen.append(envelope)
            return NotificationObservation(envelope.method, "admitted")

        client = BoundedAppServerClient(self._profile(validate_notification), ws)
        async with client:
            result = await client.request("initialize", {}, request_id=17)

        self.assertIsInstance(result, BoundedRequestResult)
        self.assertEqual(result.result, {"ok": True})
        self.assertEqual(result.notifications, (NotificationObservation("remoteControl/status/changed", "admitted"),))
        self.assertEqual(seen[0].emitted_at_ms, 1234)
        self.assertEqual(seen[0].method, "remoteControl/status/changed")
        self.assertEqual(ws.sent[0]["id"], 17)
        self.assertNotIn("jsonrpc", ws.sent[0])
        self.assertTrue(ws.closed)

    async def test_mismatched_response_id_fails_closed_without_queueing(self) -> None:
        ws = MockWebSocket([{"jsonrpc": "2.0", "id": 18, "result": {}}])
        client = BoundedAppServerClient(self._profile(), ws)
        with self.assertRaisesRegex(BoundedProtocolError, "response id"):
            await client.request("initialize", {}, request_id=17)
        self.assertTrue(ws.closed)

    async def test_response_id_type_is_part_of_strict_correlation(self) -> None:
        ws = MockWebSocket([{"jsonrpc": "2.0", "id": 17.0, "result": {}}])
        client = BoundedAppServerClient(self._profile(), ws)
        with self.assertRaisesRegex(BoundedProtocolError, "response id"):
            await client.request("initialize", {}, request_id=17)

    async def test_response_request_hybrid_is_rejected_in_strict_mode(self) -> None:
        ws = MockWebSocket([{"id": 17, "method": "item/tool/requestUserInput", "result": {"ok": True}}])
        client = BoundedAppServerClient(self._profile(), ws)
        with self.assertRaisesRegex(BoundedProtocolError, "hybrid"):
            await client.request("initialize", {}, request_id=17)

    async def test_server_request_is_not_auto_answered(self) -> None:
        ws = MockWebSocket([{"id": 19, "method": "item/tool/requestUserInput", "params": {}}])
        client = BoundedAppServerClient(self._profile(), ws)
        with self.assertRaisesRegex(BoundedProtocolError, "server-request"):
            await client.request("initialize", {}, request_id=17)
        self.assertEqual(len(ws.sent), 1)
        self.assertTrue(ws.closed)

    async def test_unknown_notification_is_rejected_before_validator(self) -> None:
        ws = MockWebSocket([{"method": "future/notification", "params": {}}])
        called = False

        def validate_notification(_envelope):
            nonlocal called
            called = True
            return NotificationObservation("future/notification", "admitted")

        client = BoundedAppServerClient(self._profile(validate_notification), ws)
        with self.assertRaisesRegex(BoundedProtocolError, "unknown"):
            await client.request("initialize", {}, request_id=17)
        self.assertFalse(called)

    async def test_overload_error_is_not_retried(self) -> None:
        ws = MockWebSocket([{"id": 17, "error": {"code": APP_SERVER_OVERLOADED}}])
        client = BoundedAppServerClient(self._profile(), ws)
        with self.assertRaisesRegex(BoundedProtocolError, "error response"):
            await client.request("initialize", {}, request_id=17)
        self.assertEqual(len(ws.sent), 1)

    async def test_initialize_sends_source_valid_initialized_and_reuses_attempt_budget(self) -> None:
        ws = MockWebSocket(
            [
                {"id": 0, "result": {"ready": True}},
                {"method": "configWarning", "params": {}, "emittedAtMs": 1234},
                {"id": 1, "result": {"profiles": []}},
            ]
        )
        validator = lambda envelope: NotificationObservation(envelope.method, "admitted")
        client = BoundedAppServerClient(
            self._profile(validator, attempt_timeout=1, request_timeout=1),
            ws,
        )
        deadline = monotonic() + 1
        initialized = await client.initialize(request_id=0, deadline=deadline)
        listed = await client.request("permissionProfile/list", {}, request_id=1, deadline=deadline)
        self.assertEqual(initialized.result, {"ready": True})
        self.assertEqual(listed.result, {"profiles": []})
        self.assertEqual(ws.sent[0]["method"], "initialize")
        self.assertEqual(ws.sent[1], {"method": "initialized"})
        self.assertEqual(ws.sent[2]["method"], "permissionProfile/list")
        self.assertEqual(listed.notifications, (NotificationObservation("configWarning", "admitted"),))

    async def test_notification_limit_applies_across_requests_on_one_connection(self) -> None:
        ws = MockWebSocket(
            [
                {"method": "configWarning", "params": {}},
                {"id": 1, "result": {}},
                {"method": "configWarning", "params": {}},
                {"id": 2, "result": {}},
            ]
        )
        validator = lambda envelope: NotificationObservation(envelope.method, "admitted")
        client = BoundedAppServerClient(self._profile(validator, max_notifications=1), ws)
        await client.request("one", {}, request_id=1)
        with self.assertRaisesRegex(BoundedProtocolError, "notification limit"):
            await client.request("two", {}, request_id=2)

    async def test_attempt_deadline_is_not_renewed_between_requests(self) -> None:
        ws = MockWebSocket([])
        client = BoundedAppServerClient(self._profile(attempt_timeout=0.1, request_timeout=1), ws)

        async def recv() -> str:
            return json.dumps({"id": ws.sent[-1]["id"], "result": {"ok": True}})

        ws.recv = recv  # type: ignore[method-assign]
        await client.request("one", {}, request_id=1)
        await asyncio.sleep(0.15)
        with self.assertRaisesRegex(BoundedProtocolError, "deadline"):
            await client.request("two", {}, request_id=2)

    async def test_explicit_deadline_cannot_extend_configured_request_cap(self) -> None:
        ws = MockWebSocket([])
        blocked = asyncio.Event()

        async def recv() -> str:
            await blocked.wait()
            return "{}"

        ws.recv = recv  # type: ignore[method-assign]
        client = BoundedAppServerClient(self._profile(request_timeout=0.01), ws)
        with self.assertRaisesRegex(BoundedProtocolError, "deadline"):
            await client.request("capped", {}, request_id=1, deadline=monotonic() + 1)

    async def test_cancellation_aborts_transport_and_terminally_closes_client(self) -> None:
        class Transport:
            def __init__(self) -> None:
                self.aborted = False

            def abort(self) -> None:
                self.aborted = True

        ws = MockWebSocket([])
        transport = Transport()
        ws.transport = transport

        async def recv() -> str:
            await asyncio.sleep(10)
            return "{}"

        ws.recv = recv  # type: ignore[method-assign]
        client = BoundedAppServerClient(self._profile(request_timeout=1), ws)
        task = asyncio.create_task(client.request("cancel", {}, request_id=1))
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(transport.aborted)
        self.assertTrue(client._closed)

    async def test_terminal_cleanup_prevents_reuse(self) -> None:
        ws = MockWebSocket([])
        client = BoundedAppServerClient(self._profile(), ws)
        await client.close()
        with self.assertRaisesRegex(BoundedProtocolError, "client is closed"):
            await client.request("after-close", {}, request_id=1)

    async def test_close_cancellation_aborts_transport_and_remains_idempotent(self) -> None:
        class Transport:
            def __init__(self) -> None:
                self.aborted = False

            def abort(self) -> None:
                self.aborted = True

        ws = MockWebSocket([])
        ws.transport = Transport()
        close_started = asyncio.Event()
        close_blocked = asyncio.Event()

        async def close() -> None:
            close_started.set()
            await close_blocked.wait()

        ws.close = close  # type: ignore[method-assign]
        client = BoundedAppServerClient(self._profile(), ws)
        task = asyncio.create_task(client.close())
        await close_started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(ws.transport.aborted)
        await client.close()
        self.assertTrue(ws.transport.aborted)

    async def test_duplicate_response_id_is_rejected(self) -> None:
        ws = MockWebSocket(
            [
                {"jsonrpc": "2.0", "id": 17, "result": {"first": True}},
                {"jsonrpc": "2.0", "id": 17, "result": {"second": True}},
            ]
        )
        client = BoundedAppServerClient(self._profile(), ws)
        first = await client.request("initialize", {}, request_id=17)
        self.assertEqual(first.result, {"first": True})
        with self.assertRaisesRegex(BoundedProtocolError, "duplicate"):
            await client.request("initialize", {}, request_id=17)

    async def test_notification_count_limit_is_enforced(self) -> None:
        ws = MockWebSocket(
            [
                {"method": "configWarning", "params": {}},
                {"method": "configWarning", "params": {}},
                {"jsonrpc": "2.0", "id": 17, "result": {}},
            ]
        )
        validator = lambda envelope: NotificationObservation(envelope.method, "admitted")
        client = BoundedAppServerClient(self._profile(validator, max_notifications=1), ws)
        with self.assertRaisesRegex(BoundedProtocolError, "notification limit"):
            await client.request("initialize", {}, request_id=17)

    async def test_request_deadline_is_shared_across_receive_and_cleanup(self) -> None:
        ws = MockWebSocket([])

        async def recv() -> str:
            await asyncio.sleep(0.05)
            return "{}"

        ws.recv = recv  # type: ignore[method-assign]
        client = BoundedAppServerClient(self._profile(request_timeout=1), ws)
        with self.assertRaisesRegex(BoundedProtocolError, "deadline"):
            await client.request("initialize", {}, request_id=17, deadline=monotonic() + 0.01)
        self.assertTrue(ws.closed)

    async def test_bounded_adapter_does_not_write_raw_ndjson(self) -> None:
        ws = MockWebSocket([{"jsonrpc": "2.0", "id": 17, "result": {"ok": True}}])
        client = BoundedAppServerClient(self._profile(), ws)
        with mock.patch("codex_ws_client.write_ndjson") as write_ndjson:
            await client.request("initialize", {"prompt": "do not log me"}, request_id=17)
        write_ndjson.assert_not_called()

    async def test_frame_and_aggregate_limits_are_enforced(self) -> None:
        frame_ws = MockWebSocket([{"jsonrpc": "2.0", "id": 17, "result": {"large": "x" * 200}}])
        frame_client = BoundedAppServerClient(self._profile(max_frame_bytes=128, max_total_bytes=512), frame_ws)
        with self.assertRaisesRegex(BoundedProtocolError, "frame limit"):
            await frame_client.request("initialize", {}, request_id=17)

        total_ws = MockWebSocket([{"jsonrpc": "2.0", "id": 17, "result": {"ok": True}}])
        total_client = BoundedAppServerClient(self._profile(max_frame_bytes=80, max_total_bytes=80), total_ws)
        with self.assertRaisesRegex(BoundedProtocolError, "aggregate byte"):
            await total_client.request("initialize", {}, request_id=17)

    def test_source_notification_envelope_rejects_jsonrpc_and_bad_timestamp(self) -> None:
        with self.assertRaises(BoundedProtocolError):
            parse_server_notification_envelope({"jsonrpc": "2.0", "method": "configWarning", "params": {}})
        with self.assertRaises(BoundedProtocolError):
            parse_server_notification_envelope({"method": "configWarning", "params": {}, "emittedAtMs": True})

    def test_bounded_profile_rejects_uri_credentials(self) -> None:
        with self.assertRaisesRegex(ValueError, "credentials"):
            BoundedClientProfile("wss://user:secret@operator.example/app-server")

    async def test_connect_uses_operator_endpoint_and_closes_deterministically(self) -> None:
        ws = MockWebSocket([])
        with mock.patch("codex_ws_client.websockets.connect", new=mock.AsyncMock(return_value=ws)) as connect:
            async with open_bounded_client(
                BoundedClientProfile("wss://operator.example/app-server", headers={"Authorization": "Bearer secret"})
            ) as client:
                self.assertIs(client.ws, ws)
        connect.assert_awaited_once()
        self.assertEqual(connect.await_args.args[0], "wss://operator.example/app-server")
        self.assertIsNone(connect.await_args.kwargs["proxy"])
        self.assertEqual(connect.await_args.kwargs["additional_headers"], {"Authorization": "Bearer secret"})
        self.assertTrue(ws.closed)


class ProtocolClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_user_input_server_request_gets_schema_valid_empty_answers(self) -> None:
        ws = MockWebSocket([])
        handled = await default_server_request_handler(
            ws,
            {"id": 9, "method": "item/tool/requestUserInput", "params": {}},
        )
        self.assertTrue(handled)
        self.assertEqual(ws.sent[0]["id"], 9)
        self.assertEqual(ws.sent[0]["result"], {"answers": {}})

    async def test_cli_protocol_core_accepts_source_shaped_response_without_jsonrpc(self) -> None:
        ws = MockWebSocket([])
        client = ProtocolClient(ws)

        async def recv() -> str:
            return json.dumps({"id": ws.sent[-1]["id"], "result": {"ok": True}})

        ws.recv = recv  # type: ignore[method-assign]
        self.assertEqual(await client.request("permissionProfile/list", {}, timeout=1), {"ok": True})

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

    def test_turn_metrics_accepts_current_nested_token_usage_shape(self) -> None:
        metrics = turn_metrics(
            {
                "tokenUsage": {
                    "total": {"inputTokens": 99, "outputTokens": 20},
                    "last": {
                        "inputTokens": 10,
                        "cachedInputTokens": 8,
                        "cacheWriteInputTokens": 1,
                        "outputTokens": 2,
                        "reasoningOutputTokens": 1,
                    },
                    "modelContextWindow": 128000,
                }
            }
        )
        self.assertEqual(
            metrics,
            {
                "input_tokens": 10,
                "output_tokens": 2,
                "cached_tokens": 8,
                "cache_write_tokens": 1,
            },
        )

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
        self.assertTrue(ws.sent[0]["params"]["excludeTurns"])
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
                self.assertEqual(resolve_default_model(), "gpt-5.1-codex")

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
