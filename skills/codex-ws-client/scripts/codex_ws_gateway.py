"""Authenticated WebSocket relay that exposes a local ``codex app-server`` remotely.

``codex app-server`` binds loopback only and has no authentication of its own.  This
gateway sits in front of it: it terminates TLS, checks a bearer token before a single
frame is forwarded, and then pumps JSON-RPC frames verbatim in both directions.  No
protocol re-modeling happens here, so every app-server method keeps working, including
server-initiated approval and elicitation requests.

Remote callers use the normal client against the gateway:

    export CODEX_GATEWAY_TOKEN=...
    python codex_ws_client.py --uri wss://host:8443 \
        --header-env "Authorization=CODEX_GATEWAY_AUTH" "your prompt"

where ``CODEX_GATEWAY_AUTH`` holds ``Bearer <token>``.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import logging
import os
import secrets
import ssl
import sys
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from time import monotonic
from typing import Any, Iterable

import websockets
from websockets.asyncio.client import connect as ws_connect
from websockets.asyncio.server import ServerConnection, serve

DEFAULT_UPSTREAM = "ws://127.0.0.1:8765"
DEFAULT_LISTEN_HOST = "0.0.0.0"
DEFAULT_LISTEN_PORT = 8443
DEFAULT_PATH = "/"
DEFAULT_MAX_SIZE = 8_000_000
DEFAULT_MAX_CONNECTIONS = 16
DEFAULT_IDLE_TIMEOUT = 900.0
DEFAULT_UPSTREAM_CONNECT_TIMEOUT = 10.0
MIN_TOKEN_LENGTH = 32

AUTH_FAILURE_LIMIT = 5
AUTH_FAILURE_WINDOW = 300.0

EXIT_SUCCESS = 0
EXIT_BAD_ARGS = 2
EXIT_LISTEN_FAILURE = 3
EXIT_SIGINT = 130

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}

log = logging.getLogger("codex-ws-gateway")


@dataclass
class AuthThrottle:
    """Per-peer lockout so a public listener cannot be brute-forced cheaply."""

    limit: int = AUTH_FAILURE_LIMIT
    window: float = AUTH_FAILURE_WINDOW
    failures: dict[str, list[float]] = field(default_factory=dict)

    def _recent(self, peer: str, now: float) -> list[float]:
        stamps = [t for t in self.failures.get(peer, []) if now - t < self.window]
        if stamps:
            self.failures[peer] = stamps
        else:
            self.failures.pop(peer, None)
        return stamps

    def locked_out(self, peer: str, now: float | None = None) -> bool:
        return len(self._recent(peer, now if now is not None else monotonic())) >= self.limit

    def record_failure(self, peer: str, now: float | None = None) -> None:
        now = now if now is not None else monotonic()
        self._recent(peer, now)
        self.failures.setdefault(peer, []).append(now)

    def clear(self, peer: str) -> None:
        self.failures.pop(peer, None)


def peer_address(connection: Any) -> str:
    remote = getattr(connection, "remote_address", None)
    if isinstance(remote, tuple) and remote:
        return str(remote[0])
    return "unknown"


def token_matches(expected: str, header_value: str | None) -> bool:
    """Constant-time bearer comparison.

    The scheme is compared case-insensitively per RFC 7235; the credential itself is
    compared with ``hmac.compare_digest`` so a wrong token leaks no timing signal.
    """
    if not header_value:
        return False
    parts = header_value.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    return hmac.compare_digest(parts[1].strip(), expected)


def origin_allowed(origin: str | None, allowed: frozenset[str]) -> bool:
    """Reject browser-originated connections unless explicitly allowlisted.

    A token in a remote caller's config is not protection against cross-site
    WebSocket hijacking, so any request carrying an ``Origin`` is refused by default.
    """
    if origin is None:
        return True
    return origin in allowed


@dataclass
class GatewayConfig:
    upstream: str
    token: str
    path: str = DEFAULT_PATH
    max_size: int = DEFAULT_MAX_SIZE
    max_connections: int = DEFAULT_MAX_CONNECTIONS
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT
    upstream_connect_timeout: float = DEFAULT_UPSTREAM_CONNECT_TIMEOUT
    allowed_origins: frozenset[str] = frozenset()


class Gateway:
    def __init__(self, config: GatewayConfig) -> None:
        self.config = config
        self.throttle = AuthThrottle()
        self.active = 0
        self._next_id = 0

    def _connection_id(self) -> str:
        self._next_id += 1
        return f"c{self._next_id}"

    async def process_request(self, connection: ServerConnection, request: Any) -> Any:
        """Gate every inbound request before the WebSocket handshake completes."""
        peer = peer_address(connection)
        path = request.path.split("?", 1)[0]

        if path == "/healthz":
            return connection.respond(HTTPStatus.OK, "ok\n")

        if path != self.config.path:
            log.warning("reject %s: unknown path %r", peer, path)
            return connection.respond(HTTPStatus.NOT_FOUND, "not found\n")

        if self.throttle.locked_out(peer):
            log.warning("reject %s: locked out after repeated auth failures", peer)
            return connection.respond(HTTPStatus.TOO_MANY_REQUESTS, "too many failed attempts\n")

        if not origin_allowed(request.headers.get("Origin"), self.config.allowed_origins):
            log.warning("reject %s: disallowed Origin %r", peer, request.headers.get("Origin"))
            return connection.respond(HTTPStatus.FORBIDDEN, "origin not allowed\n")

        if not token_matches(self.config.token, request.headers.get("Authorization")):
            self.throttle.record_failure(peer)
            log.warning("reject %s: bad or missing bearer token", peer)
            return connection.respond(HTTPStatus.UNAUTHORIZED, "unauthorized\n")

        if self.active >= self.config.max_connections:
            log.warning("reject %s: connection limit %d reached", peer, self.config.max_connections)
            return connection.respond(HTTPStatus.SERVICE_UNAVAILABLE, "connection limit reached\n")

        self.throttle.clear(peer)
        return None

    async def handle(self, client: ServerConnection) -> None:
        peer = peer_address(client)
        cid = self._connection_id()
        self.active += 1
        if is_loopback_host(peer):
            log.info("[%s] accepted %s (active=%d)", cid, peer, self.active)
        else:
            # A remote caller holding the token can now drive this host. Surface it at
            # WARNING so an unexpected peer is visible without turning on --verbose.
            log.warning(
                "[%s] remote peer %s authenticated and can now drive this host's app-server (active=%d)",
                cid,
                peer,
                self.active,
            )
        upstream = None
        try:
            try:
                upstream = await asyncio.wait_for(
                    ws_connect(self.config.upstream, max_size=self.config.max_size),
                    timeout=self.config.upstream_connect_timeout,
                )
            except (OSError, asyncio.TimeoutError, websockets.exceptions.WebSocketException) as exc:
                log.error("[%s] upstream %s unreachable: %s", cid, self.config.upstream, exc)
                await client.close(code=1011, reason="upstream unavailable")
                return

            log.info("[%s] bridged to %s", cid, self.config.upstream)
            await self._relay(cid, client, upstream)
        finally:
            if upstream is not None:
                await upstream.close()
            self.active -= 1
            log.info("[%s] closed %s (active=%d)", cid, peer, self.active)

    async def _relay(self, cid: str, client: Any, upstream: Any) -> None:
        pumps = [
            asyncio.create_task(self._pump(cid, client, upstream, "client->upstream")),
            asyncio.create_task(self._pump(cid, upstream, client, "upstream->client")),
        ]
        done, pending = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(exc, websockets.exceptions.ConnectionClosed):
                log.error("[%s] relay error: %s", cid, exc)

    async def _pump(self, cid: str, source: Any, sink: Any, direction: str) -> None:
        timeout = self.config.idle_timeout or None
        while True:
            try:
                message = await asyncio.wait_for(source.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                log.info("[%s] idle timeout on %s", cid, direction)
                await sink.close(code=1001, reason="idle timeout")
                return
            except websockets.exceptions.ConnectionClosed:
                await sink.close()
                return
            await sink.send(message)


def is_loopback_host(host: str) -> bool:
    return host in LOOPBACK_HOSTS


def security_warnings(args: argparse.Namespace, tls_enabled: bool) -> list[str]:
    """Enumerate what an operator is accepting by starting this gateway.

    Every item is a live consequence of the current flags, not boilerplate: the banner
    stays short enough to actually get read, and silent on risks that do not apply.
    """
    warnings: list[str] = [
        "Anyone holding the bearer token can make this host's codex app-server run "
        f"commands and write files as user '{current_user()}'. Treat it like an SSH key.",
        "The gateway authenticates callers; it does NOT restrict what they ask for. A "
        "remote client can request danger-full-access. Constrain the app-server's "
        "sandbox/permission policy where it is started.",
    ]
    if not tls_enabled:
        warnings.append(
            "TLS is disabled: the bearer token and every prompt, file path, and command "
            "cross the network in cleartext, readable by anything on the path."
        )
    if not is_loopback_host(args.listen_host):
        warnings.append(
            f"Bound to {args.listen_host}, so this port is reachable from other machines. "
            "A publicly routable port will be scanned within hours. Prefer 127.0.0.1 "
            "behind an SSH tunnel or WireGuard/Tailscale when that reaches far enough."
        )
    if args.allow_origin:
        warnings.append(
            "Browser origins are allowlisted (" + ", ".join(args.allow_origin) + "), so a "
            "page on those origins can drive this gateway using an ambient token."
        )
    if not args.idle_timeout:
        warnings.append("Idle timeout is disabled; abandoned connections hold slots until the process restarts.")
    return warnings


def log_security_banner(warnings: list[str]) -> None:
    log.warning("=" * 78)
    log.warning("SECURITY: this gateway publishes a local codex app-server to the network.")
    for warning in warnings:
        log.warning("-> %s", warning)
    log.warning("=" * 78)


def current_user() -> str:
    return os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"


def build_ssl_context(certfile: str, keyfile: str) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=certfile, keyfile=keyfile or None)
    return context


def resolve_token(env_var: str) -> tuple[str, str | None]:
    """Return ``(token, error)`` after reading the shared secret from the environment."""
    token = os.environ.get(env_var, "").strip()
    if not token:
        return "", (
            f"No token found in ${env_var}. Generate one with --new-token and export it "
            f"before starting the gateway; the gateway refuses to run unauthenticated."
        )
    if len(token) < MIN_TOKEN_LENGTH:
        return "", f"${env_var} must be at least {MIN_TOKEN_LENGTH} characters; got {len(token)}."
    return token, None


def validate_transport(args: argparse.Namespace) -> str | None:
    """Refuse to publish an unauthenticated-transport listener off loopback."""
    has_tls = bool(args.certfile)
    if has_tls and not Path(args.certfile).is_file():
        return f"--certfile not found: {args.certfile}"
    if args.keyfile and not Path(args.keyfile).is_file():
        return f"--keyfile not found: {args.keyfile}"
    if has_tls:
        return None
    if args.listen_host in LOOPBACK_HOSTS:
        return None
    if args.allow_plaintext:
        return None
    return (
        f"Refusing to listen on {args.listen_host} without TLS: the bearer token and every "
        "JSON-RPC frame would cross the network in the clear. Pass --certfile/--keyfile, "
        "bind 127.0.0.1 and front it with a TLS reverse proxy, or override with "
        "--allow-plaintext if a tunnel already encrypts the hop."
    )


async def run_gateway(args: argparse.Namespace, token: str) -> int:
    config = GatewayConfig(
        upstream=args.upstream,
        token=token,
        path=args.path,
        max_size=args.max_size,
        max_connections=args.max_connections,
        idle_timeout=args.idle_timeout,
        upstream_connect_timeout=args.upstream_connect_timeout,
        allowed_origins=frozenset(args.allow_origin),
    )
    gateway = Gateway(config)
    ssl_context = build_ssl_context(args.certfile, args.keyfile) if args.certfile else None
    log_security_banner(security_warnings(args, ssl_context is not None))

    try:
        async with serve(
            gateway.handle,
            args.listen_host,
            args.listen_port,
            process_request=gateway.process_request,
            ssl=ssl_context,
            max_size=args.max_size,
            ping_interval=args.ping_interval or None,
            ping_timeout=args.ping_timeout or None,
        ):
            scheme = "wss" if ssl_context else "ws"
            log.info(
                "listening on %s://%s:%d%s -> %s",
                scheme,
                args.listen_host,
                args.listen_port,
                args.path,
                args.upstream,
            )
            await asyncio.get_running_loop().create_future()
    except OSError as exc:
        log.error("cannot listen on %s:%d: %s", args.listen_host, args.listen_port, exc)
        return EXIT_LISTEN_FAILURE
    return EXIT_SUCCESS


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Authenticated WebSocket relay fronting a local codex app-server.",
    )
    parser.add_argument("--upstream", default=DEFAULT_UPSTREAM, help=f"Local app-server URI (default {DEFAULT_UPSTREAM}).")
    parser.add_argument("--listen-host", default=DEFAULT_LISTEN_HOST, help=f"Bind address (default {DEFAULT_LISTEN_HOST}).")
    parser.add_argument("--listen-port", type=int, default=DEFAULT_LISTEN_PORT, help=f"Bind port (default {DEFAULT_LISTEN_PORT}).")
    parser.add_argument("--path", default=DEFAULT_PATH, help=f"Request path clients must use (default {DEFAULT_PATH}).")
    parser.add_argument("--certfile", default="", help="TLS certificate chain. Required to bind a non-loopback host.")
    parser.add_argument("--keyfile", default="", help="TLS private key, if not bundled in --certfile.")
    parser.add_argument(
        "--allow-plaintext",
        action="store_true",
        help="Permit a non-loopback listener without TLS. Only safe when an SSH/WireGuard tunnel already encrypts the hop.",
    )
    parser.add_argument(
        "--token-env",
        default="CODEX_GATEWAY_TOKEN",
        metavar="ENV_VAR",
        help="Environment variable holding the shared bearer token (default CODEX_GATEWAY_TOKEN).",
    )
    parser.add_argument(
        "--allow-origin",
        action="append",
        default=[],
        metavar="ORIGIN",
        help="Allow a browser Origin. Repeatable. By default any request carrying Origin is rejected.",
    )
    parser.add_argument("--max-connections", type=int, default=DEFAULT_MAX_CONNECTIONS, help=f"Concurrent client cap (default {DEFAULT_MAX_CONNECTIONS}).")
    parser.add_argument("--max-size", type=int, default=DEFAULT_MAX_SIZE, help=f"Max frame size in bytes (default {DEFAULT_MAX_SIZE}).")
    parser.add_argument("--idle-timeout", type=float, default=DEFAULT_IDLE_TIMEOUT, help=f"Drop a connection after this many idle seconds; 0 disables (default {DEFAULT_IDLE_TIMEOUT}).")
    parser.add_argument("--upstream-connect-timeout", type=float, default=DEFAULT_UPSTREAM_CONNECT_TIMEOUT, help="Seconds to wait for the local app-server.")
    parser.add_argument("--ping-interval", type=float, default=20.0, help="Keepalive ping interval; 0 disables.")
    parser.add_argument("--ping-timeout", type=float, default=20.0, help="Keepalive ping timeout; 0 disables.")
    parser.add_argument("--new-token", action="store_true", help="Print a fresh random token and exit.")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    return parser.parse_args(list(argv) if argv is not None else None)


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )

    if args.new_token:
        print(secrets.token_urlsafe(32))
        return EXIT_SUCCESS

    token, token_error = resolve_token(args.token_env)
    if token_error:
        log.error("%s", token_error)
        return EXIT_BAD_ARGS

    transport_error = validate_transport(args)
    if transport_error:
        log.error("%s", transport_error)
        return EXIT_BAD_ARGS

    try:
        return asyncio.run(run_gateway(args, token))
    except KeyboardInterrupt:
        return EXIT_SIGINT


if __name__ == "__main__":
    sys.exit(main())
