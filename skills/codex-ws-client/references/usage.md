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
