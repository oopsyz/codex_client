# codex_ws_client.py Reference

This skill bundles `scripts/codex_ws_client.py`, a lightweight client for `codex app-server` over WebSocket.

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

It handles:
- `item/agentMessage/delta`
- `turn/completed`
- `turn/failed`
- approval/file-change/permissions server requests
- selected thread/tool/command/file-change notifications

## Thread behavior

Fresh thread:
- omit `--thread-id`

Persisted thread:
- default creation mode persists threads
- reuse with `--thread-id`
- resumed turns use `--resume-timeout`

Ephemeral thread:
- use `--ephemeral`
- cannot be resumed across connections

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
