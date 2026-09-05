# codex_ws_client.py Reference

This skill bundles `scripts/codex_ws_client.py`, a single-file lightweight client for `codex app-server` over WebSocket.

Protocol maintenance reference: [Codex App Server documentation](https://learn.chatgpt.com/docs/app-server).
Use it to verify app-server methods, notifications, response fields, lifecycle
semantics, and experimental API requirements before changing the client.
For model-specific cache-retention guidance, see the [OpenAI prompt-caching guide](https://developers.openai.com/api/docs/guides/prompt-caching).

## Use cases

Use it when:
- a long-lived `codex app-server` is already running
- you want lower overhead than spawning `codex exec` for every turn
- you want direct control over thread ids, timeouts, JSON output, and logging

Avoid it when:
- you need stdio transport
- you need full orchestration/job management
- you need comprehensive support for every server request type

## Protocol flow

The client does:

1. connect to the WebSocket
2. send `initialize`
3. send `initialized`
4. create or resume a thread
5. send `turn/start`
6. consume notifications until completion or failure

With `--detach`, step 6 is replaced by `thread/unsubscribe`; the client prints the thread and turn IDs and exits while the server continues the turn.

If `--cwd` is omitted, the client leaves `cwd` out of the protocol params and `codex app-server` uses its own default workspace.

It handles:
- `item/agentMessage/delta`
- `turn/completed`
- `turn/failed`
- approval/file-change/permissions server requests
- selected thread/tool/command/file-change notifications

## Thread behavior

Fresh thread:
- omit `--thread-id`
- choose exactly one of `--sandbox read-only`, `--sandbox workspace-write`,
  `--sandbox danger-full-access`, or `--permissions PROFILE_ID`
- never combine `--sandbox` with `--permissions`

Model selection:
- `--model` overrides the configured model
- if `--model` is omitted, the client reads project `.codex/config.toml` files first
- user `~/.codex/config.toml` is the fallback if no project model is set
- if the config does not define a model, the client falls back to its built-in default

Persisted thread:
- default creation mode persists threads
- reuse with `--thread-id`
- resumed turns use `--resume-timeout`
- do not pass `--sandbox` or `--permissions`; the existing thread's permission
  policy cannot change
- use `--detach` for fire-and-forget turns that will be inspected later with `--read-thread THREAD_ID --include-turns`

Ephemeral thread:
- use `--ephemeral`
- cannot be resumed across connections
- cannot be used with `--detach`

## REPL behavior

Commands:
- `/thread`
- `/new`
- `/exit`
- `/quit`

Interactive approvals:
- only available with `--repl --interactive-approvals`

## Logging

- `-v`: lifecycle and selected notification summaries
- `-vv`: raw JSON-RPC traffic
- `--ndjson-file FILE`: structured trace file
- `--summary`: stderr token and latency summary
- `--out FILE`: save final assistant text

## JSON result

`--json` emits a structured object with:
- ids and final text
- status and optional error
- effective sandbox
- notification summaries
- metrics such as latency and token counts

With `--detach --json`, the object contains `thread_id`, `turn_id`, `status: "detached"`, `turn_status`, `unsubscribe_status`, and effective `sandbox`. The detached status only means that the client unsubscribed successfully; it is not the final turn status. Plain detached output includes `SANDBOX=` as well.

The output field keeps its legacy `sandbox` name. For a thread created with
`--permissions`, it contains the selected profile id.

Use `--read-turn THREAD_ID TURN_ID` to read one persisted turn in normalized form. The result contains `thread_id`, `turn_id`, `status`, concatenated agent-message `text`, the raw `turn`, and an optional `error`. A missing turn returns `status: "not_found"`.

Correct an active turn without creating another turn or changing its binding:

```powershell
python skills/codex-ws-client/scripts/codex_ws_client.py --steer-turn THREAD_ID TURN_ID "Use the corrected scope."
```

`--steer-turn` calls `turn/steer` with the supplied `TURN_ID` as the app-server's `expectedTurnId` precondition. It does not call `thread/resume` or `turn/start`; a stale ID fails instead of steering a different active turn.

Wait for a detached or otherwise active turn without a separate read/retry loop:

```powershell
python skills/codex-ws-client/scripts/codex_ws_client.py --wait-turn THREAD_ID TURN_ID
```

This polls persisted `thread/read` state (it does not resume or subscribe to the thread) until `completed`, `failed`, or `interrupted`. It always emits normalized JSON with `wait_status` (`terminal` or `timeout`) and `poll_count`; timeout defaults to 300 seconds and returns exit code 4. Use `--wait-turn-timeout SECONDS` and `--wait-turn-poll-interval SECONDS` to tune it.

Read-only recovery and runtime diagnosis commands:

```powershell
python skills/codex-ws-client/scripts/codex_ws_client.py --list-loaded-threads
python skills/codex-ws-client/scripts/codex_ws_client.py --thread-items THREAD_ID --items-turn-id TURN_ID --items-limit 100
python skills/codex-ws-client/scripts/codex_ws_client.py --thread-turns THREAD_ID --turns-items-view full
python skills/codex-ws-client/scripts/codex_ws_client.py --background-terminals THREAD_ID
```

`--list-threads` accepts `--threads-cursor`, `--threads-limit`, sort controls,
`--model-provider`, `--source-kind`, `--archived`, `--use-state-db-only`, and
parent/ancestor filters. Use the returned `nextCursor` with the next request.
`--thread-items`, `--thread-turns`, and `--background-terminals` likewise expose
their server cursors and page sizes. `--interrupt-turn THREAD_ID TURN_ID`
explicitly requests cancellation of an in-flight turn; `--set-thread-name
THREAD_ID NAME` sets a user-facing correlation name and is not an engine identity.

Lifecycle commands for release and smoke-workspace cleanup:

```powershell
python skills/codex-ws-client/scripts/codex_ws_client.py --list-loaded-threads
python skills/codex-ws-client/scripts/codex_ws_client.py --list-background-terminals THREAD_ID
python skills/codex-ws-client/scripts/codex_ws_client.py --terminate-background-terminal THREAD_ID PROCESS_ID
python skills/codex-ws-client/scripts/codex_ws_client.py --clean-background-terminals THREAD_ID
python skills/codex-ws-client/scripts/codex_ws_client.py --unsubscribe-thread THREAD_ID
```

`--list-background-terminals` is the preferred name for the existing
`--background-terminals` command. `PROCESS_ID` is the app-server process ID
returned by the list request, not an operating-system PID. `--unsubscribe-thread`
only removes this connection's subscription; a fresh one-shot CLI connection
usually returns `notSubscribed`. Use `--unload-thread` for the complete
interrupt, clean, unsubscribe, and no-subscriber grace-period workflow.

Archive a thread after the engine has durably recorded its review bundle:

```powershell
python skills/codex-ws-client/scripts/codex_ws_client.py --archive-thread THREAD_ID
```

This waits for and returns the server's matching `thread/archived` notification.
`--unarchive-thread THREAD_ID` is reserved for explicit operator recovery.
`--delete-thread THREAD_ID` permanently removes the server-side thread log and
is never used as routine cleanup.

`--unload-thread THREAD_ID` is the lifecycle teardown operation. It opens the
thread without adding a prompt, interrupts every `inProgress` turn returned by
`thread/read`, waits for each cancellation notification, calls
`thread/backgroundTerminals/clean`, then calls `thread/unsubscribe`. The
unsubscribe response is authoritative about whether this connection was
subscribed; only an `unsubscribed` response starts the client-side wait. It then
keeps the connection open for the app-server's 30-minute no-subscriber
inactivity grace period (`--unload-grace-period` overrides this; `0` skips it).
A returned `unload_status: "thread_closed"` confirms that the server unloaded
the thread. `grace_period_elapsed` means only that the client waited: another
subscriber or new thread activity can prevent the server from unloading it.

## Bounded reusable adapter

Non-conversational consumers should use the explicit bounded profile instead of
the CLI workflow or the conversational `ProtocolClient`:

```python
from codex_ws_client import (
    BoundedClientProfile,
    NotificationObservation,
    open_bounded_client,
)


def admit(notification):
    if notification.method not in {"configWarning", "remoteControl/status/changed"}:
        raise ValueError("notification not admitted")
    return NotificationObservation(notification.method, "admitted")


profile = BoundedClientProfile(
    "wss://operator-supplied-endpoint",
    request_timeout=30,
    max_frame_bytes=1_048_576,
    max_total_bytes=8_000_000,
    max_notifications=8,
    notification_validator=admit,
)

async with open_bounded_client(profile) as client:
    response = await client.request("initialize", {}, request_id=1)
```

This API owns one explicit WebSocket connection and returns only the correlated
response plus sanitized `NotificationObservation` values. It validates the
current flattened `ServerNotificationEnvelope` (`method`, `params`, optional
`emittedAtMs`), uses one shared request deadline, enforces frame/aggregate byte
and notification-count limits, never retries or buffers unrelated messages,
does not answer server requests, and does not write raw tracing output. The
validator is the only admission hook; callers retain responsibility for their
own allowed-method and parameter policy. It does not select models, permission
profiles, workspaces, or OA governance state.

## Known limits

- WebSocket only
- single-process CLI design
- not a full protocol framework
- Windows graceful interrupt of in-flight turns remains limited
- some server-request families are explicitly rejected rather than fully implemented

## Server prerequisite

Typical server command:

```powershell
codex app-server --listen ws://127.0.0.1:8765
```

## Remote access via the gateway

`codex app-server` binds loopback and has no authentication, so it must never be bound
to a public interface directly. `scripts/codex_ws_gateway.py` fronts it with TLS and a
bearer token, then relays JSON-RPC frames verbatim in both directions — every method
keeps working, including server-initiated approval and elicitation requests.

### Read before exposing a host

The gateway publishes an agent that executes code. Understand these before running it:

- **The token is equivalent to shell access.** Anyone holding it can make the app-server
  run commands and write files as the user who started it. Treat it like an SSH key:
  rotate it, never put it in argv, a URL, or a committed file.
- **The gateway authenticates callers; it does not restrict them.** A remote client can
  request `danger-full-access` exactly like a local one. Constrain the sandbox and
  permission policy where the app-server is started — the gateway will not do it for you.
- **Plaintext exposes everything.** Without TLS, the token plus every prompt, file path,
  and command is readable by anything on the path. The gateway refuses to bind a
  non-loopback host without `--certfile` unless you pass `--allow-plaintext`, and the
  client prints a warning when `--uri` is a remote `ws://` address.
- **A public port gets scanned within hours.** The token is then the entire perimeter.
  Prefer loopback plus a tunnel whenever that reaches far enough.
- **Every prompt runs in the host's filesystem**, not the caller's. `--cwd` and
  `--runtime-workspace-root` refer to paths on the gateway host.

The gateway prints a security banner at startup listing whichever of these apply to the
flags you actually passed, and logs a `WARNING` line each time a non-loopback peer
authenticates, so an unexpected client is visible without `--verbose`.

Start the gateway:

```powershell
$env:CODEX_GATEWAY_TOKEN = (python scripts/codex_ws_gateway.py --new-token)
python scripts/codex_ws_gateway.py --certfile fullchain.pem --keyfile privkey.pem
```

Connect from a remote machine — the token goes through `--header-env` so it never
enters argv or shell history:

```powershell
$env:CODEX_GATEWAY_AUTH = "Bearer <token>"
python scripts/codex_ws_client.py --uri wss://host:8443 `
  --header-env "Authorization=CODEX_GATEWAY_AUTH" "your prompt"
```

For a self-signed certificate, point the client at it with `SSL_CERT_FILE=<cert.pem>`;
Python's default SSL context honors that variable.

Defaults and guardrails:

| Flag | Default | Notes |
| --- | --- | --- |
| `--listen-host` / `--listen-port` | `0.0.0.0` / `8443` | |
| `--upstream` | `ws://127.0.0.1:8765` | the local app-server |
| `--path` | `/` | other paths get 404 before auth runs |
| `--token-env` | `CODEX_GATEWAY_TOKEN` | 32 chars minimum; refuses to start if unset |
| `--certfile` / `--keyfile` | none | required to bind a non-loopback host |
| `--allow-plaintext` | off | override TLS only when a tunnel already encrypts the hop |
| `--allow-origin` | none | any request carrying `Origin` is rejected by default |
| `--max-connections` | 16 | excess clients get 503 |
| `--idle-timeout` | 900s | 0 disables |
| `--max-size` | 8 MB | frame ceiling |

Rejections happen during `process_request`, before the handshake completes and before
any byte reaches the app-server. Five failed auth attempts lock a peer out for five
minutes (429). `GET /healthz` returns 200 without a token, for reverse-proxy probes.

Exit codes: `0` clean shutdown, `2` bad configuration (missing token, TLS refusal,
missing cert), `3` cannot bind, `130` SIGINT.

Two lower-exposure alternatives worth preferring when they fit: bind the gateway to
`127.0.0.1` and reach it over an SSH tunnel or WireGuard/Tailscale, or terminate TLS at
a reverse proxy and keep the gateway on loopback behind it.
