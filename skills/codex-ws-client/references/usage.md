# codex_ws_client.py Reference

This skill bundles `scripts/codex_ws_client.py`, a single-file lightweight client for `codex app-server` over WebSocket.

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

Model selection:
- `--model` overrides the configured model
- if `--model` is omitted, the client reads project `.codex/config.toml` files first
- user `~/.codex/config.toml` is the fallback if no project model is set
- if the config does not define a model, the client falls back to its built-in default

Persisted thread:
- default creation mode persists threads
- reuse with `--thread-id`
- resumed turns use `--resume-timeout`
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
- notification summaries
- metrics such as latency and token counts

With `--detach --json`, the object contains `thread_id`, `turn_id`, `status: "detached"`, `turn_status`, and `unsubscribe_status`. The detached status only means that the client unsubscribed successfully; it is not the final turn status.

Use `--read-turn THREAD_ID TURN_ID` to read one persisted turn in normalized form. The result contains `thread_id`, `turn_id`, `status`, concatenated agent-message `text`, the raw `turn`, and an optional `error`. A missing turn returns `status: "not_found"`.

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
