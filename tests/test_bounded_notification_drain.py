"""Offline wire regressions for opt-in noncollecting bounded observation."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import unittest
from unittest import mock
import weakref

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/codex-ws-client/scripts"))
import codex_ws_client as client


def event(method="item/started", **fields):
    return {"method": method, "params": {}, **fields}


class Wire:
    def __init__(self, frames):
        self.frames = iter(frames)
        self.sent = []
        self.closed = False
        self.received = 0

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        frame = next(self.frames)
        self.received += 1
        if isinstance(frame, BaseException):
            raise frame
        return frame if isinstance(frame, str) else json.dumps(frame)

    async def close(self):
        self.closed = True

    def fail_connection(self):
        self.closed = True


def admit(envelope):
    return client.NotificationObservation(envelope.method, "admitted")


class DrainTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self, frames, **overrides):
        options = dict(notification_mode="drain", notification_validator=admit)
        options.update(overrides)
        wire = Wire(frames)
        bounded = client.BoundedAppServerClient(
            client.BoundedClientProfile("ws://offline.invalid", **options), wire,
        )
        return bounded, wire

    def test_mode_is_explicit_strict_and_preserves_default_profile(self):
        default = client.BoundedClientProfile("ws://offline.invalid")
        self.assertEqual(default.notification_mode, "collect")
        self.assertEqual(default.max_notifications, 8)
        for value in (None, True, 0, "", "DRAIN", "discard", [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "notification_mode"):
                client.BoundedClientProfile("ws://offline.invalid", notification_mode=value)
        for field in ("max_notifications", "max_frame_bytes", "max_total_bytes"):
            for value in (None, 0, -1, True, float("inf")):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    client.BoundedClientProfile("ws://offline.invalid", notification_mode="drain", **{field: value})
        for field in ("attempt_timeout", "request_timeout", "connect_timeout", "close_timeout"):
            for value in (None, 0, -1, True, float("inf"), float("nan")):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    client.BoundedClientProfile("ws://offline.invalid", notification_mode="drain", **{field: value})

    async def test_more_than_256_one_rpc_and_across_rpcs_without_retained_observations(self):
        for batches in ((600,), (200, 200, 200)):
            with self.subTest(batches=batches):
                previous = None
                validated = 0

                def validate(envelope):
                    nonlocal previous, validated
                    if previous is not None:
                        self.assertIsNone(previous())
                    observation = admit(envelope)
                    previous = weakref.ref(observation)
                    validated += 1
                    return observation

                def frames():
                    for request_id, size in enumerate(batches, 1):
                        for _ in range(size):
                            yield event(params={"text": "transient payload"})
                        yield {"id": request_id, "result": {"exact": [request_id, None, True]}}

                bounded, wire = self.make_client(frames(), notification_validator=validate)
                original = bounded._handle_bounded_notification

                async def inspect_sink(ws, message, verbosity, observations):
                    self.assertIsNone(observations)  # Not an empty-but-growing hidden list.
                    return await original(ws, message, verbosity, observations)

                bounded._handle_bounded_notification = inspect_sink
                with mock.patch.object(client, "write_ndjson") as trace:
                    for request_id in range(1, len(batches) + 1):
                        result = await bounded.request("thread/read", {}, request_id=request_id)
                        self.assertEqual(result.result, {"exact": [request_id, None, True]})
                        self.assertEqual(result.notifications, ())
                        self.assertIsNone(previous())
                    trace.assert_not_called()
                self.assertEqual(validated, sum(batches))
                self.assertEqual(bounded._notification_count, 0)
                self.assertIsNone(bounded._core.pending_messages)
                self.assertEqual(len(wire.sent), len(batches))
                self.assertFalse(wire.closed)

    async def test_collect_still_accepts_256_and_rejects_257_across_requests(self):
        calls = 0

        def validate(envelope):
            nonlocal calls
            calls += 1
            return admit(envelope)

        def frames():
            for request_id in (1, 2):
                for _ in range(128):
                    yield event()
                yield {"id": request_id, "result": {"ok": request_id}}
            yield event()
            yield {"id": 3, "result": {"unread": True}}

        # Omit notification_mode, as existing callers do.
        wire = Wire(frames())
        bounded = client.BoundedAppServerClient(client.BoundedClientProfile(
            "ws://offline.invalid", max_notifications=256, notification_validator=validate,
        ), wire)
        for request_id in (1, 2):
            result = await bounded.request("thread/read", {}, request_id=request_id)
            self.assertEqual(result.notifications, (client.NotificationObservation("item/started", "admitted"),) * 128)
        with self.assertRaisesRegex(client.BoundedProtocolError, "^App Server notification limit exceeded$"):
            await bounded.request("thread/read", {}, request_id=3)
        self.assertEqual(calls, 256)
        self.assertEqual(bounded._notification_count, 256)
        self.assertEqual(len(wire.sent), 3)
        self.assertTrue(wire.closed)

    async def test_drain_does_not_relax_envelope_or_source_and_profile_allowlists(self):
        cases = [
            (event("future/unknown"), "unknown"),
            (event(params=[]), "params are invalid"),
            (event(emittedAtMs=True), "timestamp is invalid"),
            (event(extra="not allowed"), "unexpected fields"),
            ({"method": ["item/started"], "params": {}}, "method is invalid"),
            ("{invalid JSON", "request failed"),
            ("[]", "request failed"),
        ]
        for frame, expected in cases:
            with self.subTest(expected=expected):
                validator = mock.Mock(side_effect=admit)
                bounded, wire = self.make_client([frame], notification_validator=validator)
                with self.assertRaisesRegex(client.BoundedProtocolError, expected):
                    await bounded.request("thread/read", {}, request_id=1)
                validator.assert_not_called()
                self.assertTrue(wire.closed)
                self.assertEqual(len(wire.sent), 1)
        for frame, allowed in ((event(), frozenset()), (event("future/unknown"), {"future/unknown"})):
            bounded, wire = self.make_client([frame], known_notification_methods=allowed)
            with self.assertRaisesRegex(client.BoundedProtocolError, "unknown"):
                await bounded.request("thread/read", {}, request_id=1)
            self.assertTrue(wire.closed)

    async def test_drain_still_requires_admission_and_valid_observation(self):
        for validator, expected in (
            (None, "was not admitted"),
            (lambda _: None, "invalid observation"),
            (lambda _: client.NotificationObservation("item/completed", "ok"), "invalid observation"),
            (mock.Mock(side_effect=ValueError("secret")), "was rejected"),
            (mock.Mock(side_effect=client.BoundedProtocolError("caller rejection")), "caller rejection"),
        ):
            with self.subTest(expected=expected):
                bounded, wire = self.make_client([event()], notification_validator=validator)
                with self.assertRaisesRegex(client.BoundedProtocolError, expected):
                    await bounded.request("thread/read", {}, request_id=1)
                self.assertTrue(wire.closed)

    async def test_drain_preserves_strict_correlation_and_does_not_answer_server_requests(self):
        cases = [
            ({"id": 2, "result": {}}, "response id"),
            ({"id": 1.0, "result": {}}, "response id"),
            ({"id": 1, "result": {}, "error": {}}, "exactly one"),
            ({"id": 1, "method": "thread/read", "result": {}}, "hybrid"),
            ({"id": 2, "method": "item/tool/requestUserInput", "params": {}}, "server-request"),
        ]
        for frame, expected in cases:
            with self.subTest(expected=expected):
                bounded, wire = self.make_client([event(), frame])
                with self.assertRaisesRegex(client.BoundedProtocolError, expected):
                    await bounded.request("thread/read", {}, request_id=1)
                self.assertEqual(len(wire.sent), 1)
                self.assertIsNone(bounded._core.pending_messages)
                self.assertTrue(wire.closed)

    async def test_drain_preserves_frame_and_connection_wide_bidirectional_byte_limits(self):
        bounded, wire = self.make_client([event(params={"text": "x" * 1000})],
                                        max_frame_bytes=256, max_total_bytes=512)
        with self.assertRaisesRegex(client.BoundedProtocolError, "frame limit"):
            await bounded.request("thread/read", {}, request_id=1)
        self.assertTrue(wire.closed)
        # Requests and responses both consume the same connection-wide budget.
        bounded, wire = self.make_client(
            [event(), {"id": 1, "result": {}}, event(), {"id": 2, "result": {}}],
            max_frame_bytes=128, max_total_bytes=200,
        )
        await bounded.request("thread/read", {}, request_id=1)
        first_bytes = bounded._core.total_bytes
        self.assertGreater(first_bytes, len(json.dumps(event()).encode()))
        with self.assertRaisesRegex(client.BoundedProtocolError, "aggregate byte"):
            await bounded.request("thread/read", {}, request_id=2)
        self.assertTrue(wire.closed)
        self.assertEqual(len(wire.sent), 2)

    async def test_drain_preserves_request_and_nonrenewing_attempt_deadlines(self):
        for scope in ("request", "attempt"):
            with self.subTest(scope=scope):
                now = 100.0

                def frames():
                    nonlocal now
                    for _ in range(300):
                        now += 0.0001
                        yield event()
                    if scope == "attempt":
                        yield {"id": 1, "result": {"first": True}}
                    while True:
                        now += 0.0001
                        yield event()

                with mock.patch.object(client, "monotonic", side_effect=lambda: now):
                    bounded, wire = self.make_client(
                        frames(), attempt_timeout=0.035 if scope == "attempt" else 10,
                        request_timeout=0.035 if scope == "request" else 10,
                    )
                    if scope == "attempt":
                        await bounded.request("thread/read", {}, request_id=1)
                    with self.assertRaisesRegex(client.BoundedProtocolError, "deadline exceeded"):
                        await bounded.request("thread/read", {}, request_id=2, deadline=1000.0)
                self.assertTrue(wire.closed)
                self.assertGreater(wire.received, 256)
                self.assertLess(wire.received, 360)
                self.assertEqual(len(wire.sent), 2 if scope == "attempt" else 1)

    async def test_drain_preserves_rpc_error_policy_and_never_replays(self):
        error = {"id": 1, "error": {"code": client.APP_SERVER_OVERLOADED, "message": "busy"}}
        for preserve in (False, True):
            with self.subTest(preserve=preserve):
                bounded, wire = self.make_client([event(), error, event(), {"id": 2, "result": {"ok": True}}],
                                                preserve_rpc_errors=preserve)
                with self.assertRaises(client.BoundedRpcError if preserve else client.BoundedProtocolError) as failure:
                    await bounded.request("thread/read", {}, request_id=1)
                self.assertEqual(len(wire.sent), 1)
                self.assertEqual(wire.closed, not preserve)
                if preserve:
                    self.assertEqual(failure.exception.rpc_response, error)
                    result = await bounded.request("thread/read", {}, request_id=2)
                    self.assertEqual(result.result, {"ok": True})
                    self.assertEqual(result.notifications, ())
                    with self.assertRaisesRegex(client.BoundedProtocolError, "duplicate"):
                        await bounded.request("thread/read", {}, request_id=1)
                    self.assertEqual(len(wire.sent), 2)
                else:
                    with self.assertRaisesRegex(client.BoundedProtocolError, "client is closed"):
                        await bounded.request("thread/read", {}, request_id=2)
                    self.assertEqual(len(wire.sent), 1)

    async def test_drain_preserves_disconnect_classification_without_retry(self):
        bounded, wire = self.make_client([event(), OSError("private transport detail")])
        with self.assertRaisesRegex(client.BoundedProtocolError, "^bounded App Server request failed$"):
            await bounded.request("thread/read", {}, request_id=1)
        self.assertTrue(wire.closed)
        self.assertEqual(len(wire.sent), 1)

    async def test_drain_cancellation_aborts_and_terminally_closes_without_replay(self):
        bounded, wire = self.make_client([event()])
        waiting = asyncio.Event()
        blocked = asyncio.Event()
        original_recv = wire.recv

        async def recv():
            if wire.received == 0:
                return await original_recv()
            waiting.set()
            await blocked.wait()

        wire.recv = recv
        task = asyncio.create_task(bounded.request("thread/read", {}, request_id=1))
        await asyncio.wait_for(waiting.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(wire.closed)
        with self.assertRaisesRegex(client.BoundedProtocolError, "client is closed"):
            await bounded.request("thread/read", {}, request_id=2)
        self.assertEqual(len(wire.sent), 1)


if __name__ == "__main__":
    unittest.main()
