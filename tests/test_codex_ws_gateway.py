from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import websockets
from websockets.asyncio.client import connect as ws_connect
from websockets.asyncio.server import serve

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "skills" / "codex-ws-client" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from codex_ws_gateway import (  # noqa: E402
    EXIT_BAD_ARGS,
    AuthThrottle,
    Gateway,
    GatewayConfig,
    log_security_banner,
    origin_allowed,
    parse_args,
    resolve_token,
    security_warnings,
    token_matches,
    validate_transport,
)

sys.path.insert(0, str(SCRIPT_DIR))
from codex_ws_client import insecure_remote_uri_warning  # noqa: E402


class InsecureClientUriWarningTest(unittest.TestCase):
    def test_no_warning_for_local_or_encrypted_connections(self) -> None:
        self.assertIsNone(insecure_remote_uri_warning("ws://127.0.0.1:8765", {}))
        self.assertIsNone(insecure_remote_uri_warning("ws://localhost:8765", {}))
        self.assertIsNone(insecure_remote_uri_warning("wss://host.example:8443", {"Authorization": "Bearer x"}))

    def test_warns_on_plaintext_remote_uri(self) -> None:
        warning = insecure_remote_uri_warning("ws://host.example:8443", {})
        self.assertIn("unencrypted connection to a remote host", warning or "")

    def test_calls_out_the_exposed_authorization_header(self) -> None:
        warning = insecure_remote_uri_warning("ws://host.example:8443", {"authorization": "Bearer x"})
        self.assertIn("Authorization header", warning or "")

TOKEN = "t" * 40


class TokenMatchTest(unittest.TestCase):
    def test_accepts_bearer_scheme_case_insensitively(self) -> None:
        self.assertTrue(token_matches(TOKEN, f"Bearer {TOKEN}"))
        self.assertTrue(token_matches(TOKEN, f"bearer {TOKEN}"))

    def test_rejects_wrong_token_missing_header_and_wrong_scheme(self) -> None:
        self.assertFalse(token_matches(TOKEN, f"Bearer {'x' * 40}"))
        self.assertFalse(token_matches(TOKEN, None))
        self.assertFalse(token_matches(TOKEN, ""))
        self.assertFalse(token_matches(TOKEN, TOKEN))
        self.assertFalse(token_matches(TOKEN, f"Basic {TOKEN}"))


class OriginTest(unittest.TestCase):
    def test_absent_origin_is_allowed(self) -> None:
        self.assertTrue(origin_allowed(None, frozenset()))

    def test_browser_origin_rejected_unless_allowlisted(self) -> None:
        self.assertFalse(origin_allowed("https://evil.example", frozenset()))
        self.assertTrue(origin_allowed("https://ok.example", frozenset({"https://ok.example"})))


class ThrottleTest(unittest.TestCase):
    def test_locks_out_after_limit_and_expires_with_window(self) -> None:
        throttle = AuthThrottle(limit=3, window=100.0)
        for i in range(3):
            throttle.record_failure("1.2.3.4", now=float(i))
        self.assertTrue(throttle.locked_out("1.2.3.4", now=3.0))
        self.assertFalse(throttle.locked_out("1.2.3.4", now=200.0))

    def test_success_clears_failures(self) -> None:
        throttle = AuthThrottle(limit=2, window=100.0)
        throttle.record_failure("1.2.3.4", now=0.0)
        throttle.record_failure("1.2.3.4", now=1.0)
        throttle.clear("1.2.3.4")
        self.assertFalse(throttle.locked_out("1.2.3.4", now=2.0))

    def test_peers_are_tracked_independently(self) -> None:
        throttle = AuthThrottle(limit=2, window=100.0)
        throttle.record_failure("1.1.1.1", now=0.0)
        throttle.record_failure("1.1.1.1", now=1.0)
        self.assertTrue(throttle.locked_out("1.1.1.1", now=2.0))
        self.assertFalse(throttle.locked_out("2.2.2.2", now=2.0))


class TokenResolutionTest(unittest.TestCase):
    def test_missing_and_short_tokens_are_refused(self) -> None:
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {}, clear=True):
            _, error = resolve_token("CODEX_GATEWAY_TOKEN")
            self.assertIn("No token found", error or "")
        with mock.patch.dict(os.environ, {"CODEX_GATEWAY_TOKEN": "short"}, clear=True):
            _, error = resolve_token("CODEX_GATEWAY_TOKEN")
            self.assertIn("at least", error or "")
        with mock.patch.dict(os.environ, {"CODEX_GATEWAY_TOKEN": TOKEN}, clear=True):
            token, error = resolve_token("CODEX_GATEWAY_TOKEN")
            self.assertIsNone(error)
            self.assertEqual(token, TOKEN)


class TransportValidationTest(unittest.TestCase):
    @staticmethod
    def _args(**kwargs: object) -> SimpleNamespace:
        base = {"listen_host": "0.0.0.0", "certfile": "", "keyfile": "", "allow_plaintext": False}
        base.update(kwargs)
        return SimpleNamespace(**base)

    def test_public_bind_without_tls_is_refused(self) -> None:
        error = validate_transport(self._args())
        self.assertIn("without TLS", error or "")

    def test_loopback_and_explicit_override_are_allowed(self) -> None:
        self.assertIsNone(validate_transport(self._args(listen_host="127.0.0.1")))
        self.assertIsNone(validate_transport(self._args(allow_plaintext=True)))

    def test_missing_cert_file_is_reported(self) -> None:
        error = validate_transport(self._args(certfile="/nope/cert.pem"))
        self.assertIn("--certfile not found", error or "")


class RelayEndToEndTest(unittest.IsolatedAsyncioTestCase):
    """Boot a stub app-server plus the gateway on loopback and drive real traffic."""

    async def asyncSetUp(self) -> None:
        async def upstream_handler(ws):
            async for raw in ws:
                message = json.loads(raw)
                if message.get("method") == "ping":
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {"pong": True}}))
                    # Server-initiated request, to prove both directions relay.
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": "srv-1", "method": "approval/request"}))

        self.upstream = await serve(upstream_handler, "127.0.0.1", 0)
        upstream_port = self.upstream.sockets[0].getsockname()[1]

        self.gateway = Gateway(
            GatewayConfig(upstream=f"ws://127.0.0.1:{upstream_port}", token=TOKEN, idle_timeout=5.0)
        )
        self.server = await serve(
            self.gateway.handle,
            "127.0.0.1",
            0,
            process_request=self.gateway.process_request,
        )
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self) -> None:
        self.server.close()
        await self.server.wait_closed()
        self.upstream.close()
        await self.upstream.wait_closed()

    def uri(self, path: str = "/") -> str:
        return f"ws://127.0.0.1:{self.port}{path}"

    async def test_authorized_client_relays_both_directions(self) -> None:
        async with ws_connect(self.uri(), additional_headers={"Authorization": f"Bearer {TOKEN}"}) as ws:
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": "1", "method": "ping", "params": {}}))
            response = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            self.assertEqual(response["result"], {"pong": True})
            server_request = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            self.assertEqual(server_request["method"], "approval/request")

    async def test_missing_and_wrong_token_are_rejected_with_401(self) -> None:
        for headers in ({}, {"Authorization": f"Bearer {'x' * 40}"}):
            with self.assertRaises(websockets.exceptions.InvalidStatus) as ctx:
                await ws_connect(self.uri(), additional_headers=headers)
            self.assertEqual(ctx.exception.response.status_code, 401)

    async def test_browser_origin_is_rejected(self) -> None:
        with self.assertRaises(websockets.exceptions.InvalidStatus) as ctx:
            await ws_connect(
                self.uri(),
                additional_headers={"Authorization": f"Bearer {TOKEN}", "Origin": "https://evil.example"},
            )
        self.assertEqual(ctx.exception.response.status_code, 403)

    async def test_unknown_path_is_rejected_before_auth(self) -> None:
        with self.assertRaises(websockets.exceptions.InvalidStatus) as ctx:
            await ws_connect(self.uri("/admin"), additional_headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(ctx.exception.response.status_code, 404)

    async def test_repeated_failures_trigger_lockout(self) -> None:
        for _ in range(5):
            with self.assertRaises(websockets.exceptions.InvalidStatus):
                await ws_connect(self.uri(), additional_headers={"Authorization": "Bearer nope"})
        with self.assertRaises(websockets.exceptions.InvalidStatus) as ctx:
            await ws_connect(self.uri(), additional_headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(ctx.exception.response.status_code, 429)

    async def test_connection_limit_is_enforced(self) -> None:
        self.gateway.config.max_connections = 1
        async with ws_connect(self.uri(), additional_headers={"Authorization": f"Bearer {TOKEN}"}):
            await asyncio.sleep(0.05)
            with self.assertRaises(websockets.exceptions.InvalidStatus) as ctx:
                await ws_connect(self.uri(), additional_headers={"Authorization": f"Bearer {TOKEN}"})
            self.assertEqual(ctx.exception.response.status_code, 503)

    async def test_health_endpoint_needs_no_token(self) -> None:
        with self.assertRaises(websockets.exceptions.InvalidStatus) as ctx:
            await ws_connect(self.uri("/healthz"))
        self.assertEqual(ctx.exception.response.status_code, 200)


class SecurityWarningTest(unittest.TestCase):
    @staticmethod
    def _args(**kwargs: object) -> SimpleNamespace:
        base = {"listen_host": "127.0.0.1", "allow_origin": [], "idle_timeout": 900.0}
        base.update(kwargs)
        return SimpleNamespace(**base)

    def test_token_and_sandbox_warnings_are_unconditional(self) -> None:
        text = " ".join(security_warnings(self._args(), tls_enabled=True))
        self.assertIn("bearer token", text)
        self.assertIn("danger-full-access", text)

    def test_tls_warning_only_when_disabled(self) -> None:
        self.assertTrue(any("cleartext" in w for w in security_warnings(self._args(), tls_enabled=False)))
        self.assertFalse(any("cleartext" in w for w in security_warnings(self._args(), tls_enabled=True)))

    def test_public_bind_warning_only_off_loopback(self) -> None:
        public = security_warnings(self._args(listen_host="0.0.0.0"), tls_enabled=True)
        self.assertTrue(any("reachable from other machines" in w for w in public))
        loopback = security_warnings(self._args(), tls_enabled=True)
        self.assertFalse(any("reachable from other machines" in w for w in loopback))

    def test_origin_and_idle_warnings_are_conditional(self) -> None:
        with_origin = security_warnings(self._args(allow_origin=["https://a.example"]), tls_enabled=True)
        self.assertTrue(any("https://a.example" in w for w in with_origin))
        no_idle = security_warnings(self._args(idle_timeout=0), tls_enabled=True)
        self.assertTrue(any("Idle timeout is disabled" in w for w in no_idle))

    def test_banner_emits_every_warning_at_warning_level(self) -> None:
        warnings = security_warnings(self._args(listen_host="0.0.0.0"), tls_enabled=False)
        with self.assertLogs("codex-ws-gateway", level="WARNING") as captured:
            log_security_banner(warnings)
        joined = " ".join(captured.output)
        self.assertIn("SECURITY", joined)
        for warning in warnings:
            self.assertIn(warning[:40], joined)


class RemotePeerLoggingTest(unittest.IsolatedAsyncioTestCase):
    async def test_remote_peer_acceptance_logs_a_warning(self) -> None:
        gateway = Gateway(GatewayConfig(upstream="ws://127.0.0.1:1", token=TOKEN, upstream_connect_timeout=0.05))

        class FakeClient:
            remote_address = ("203.0.113.9", 51000)

            async def close(self, code: int = 1000, reason: str = "") -> None:
                return None

        with self.assertLogs("codex-ws-gateway", level="WARNING") as captured:
            await gateway.handle(FakeClient())
        self.assertTrue(any("remote peer 203.0.113.9 authenticated" in line for line in captured.output))


class ArgsTest(unittest.TestCase):
    def test_defaults_bind_public_and_target_local_app_server(self) -> None:
        args = parse_args([])
        self.assertEqual(args.listen_host, "0.0.0.0")
        self.assertEqual(args.upstream, "ws://127.0.0.1:8765")
        self.assertEqual(args.token_env, "CODEX_GATEWAY_TOKEN")


if __name__ == "__main__":
    unittest.main()
