# codex_client

This repository contains `codex_ws_client.py`, a lightweight client for `codex app-server` over WebSocket.

The script lives at `skills/codex-ws-client/scripts/codex_ws_client.py`.

It is intended for agents or scripts that need to:

- send a prompt to a running Codex app-server
- reuse a persisted thread with `--thread-id`
- stream or buffer assistant output
- get machine-readable JSON output
- use REPL mode for repeated prompts on one connection
- inspect richer server behavior through stderr logs or NDJSON traces

## When To Use It

Use this script when:

- a long-lived `codex app-server` is already running
- you want lower overhead than spawning `codex exec` for every turn
- you want direct control over thread ids, timeouts, JSON output, and logging

Do not use it if:

- you need stdio transport instead of WebSocket
- you need full job/session orchestration like a larger wrapper tool
- you need robust interactive approvals outside REPL mode

## Transport

This client talks only to:

- `codex app-server --listen ws://HOST:PORT`

Default URI:

```text
ws://127.0.0.1:8765
```

## Core Behavior

The client uses this protocol flow:

1. connect to the WebSocket
2. send `initialize`
3. send `initialized`
4. create or resume a thread
5. send `turn/start`
6. consume streamed notifications until the turn finishes

It handles:

- `item/agentMessage/delta`
- `turn/completed`
- `turn/failed`
- approval/file-change/permissions server requests
- selected thread/tool/command/file-change notifications

## Thread Model

Fresh thread:

- if `--thread-id` is omitted, the client creates a new thread

Resumed thread:

- if `--thread-id` is provided, the client calls `thread/resume`
- resumed turns use `--resume-timeout`

Persistence:

- threads are persisted by default
- `--ephemeral` disables persistence
- `--thread-id` only makes sense for non-ephemeral threads

Important:

- `--ephemeral` threads cannot be resumed across connections
- if a resumed thread cannot be loaded, one-shot mode fails fast
- in REPL mode, some stale-thread cases may fall back to a new thread

## Output Modes

Plain text:

- default mode streams deltas to stdout

Buffered text:

- `--no-stream` prints the final assistant text once at end of turn

JSON:

- `--json` prints a structured JSON object to stdout
- this is the best mode for another LLM or tool to consume

Current JSON shape includes:

- `thread_id`
- `turn_id`
- `status`
- `text`
- optional `error`
- optional `notifications`
- optional `metrics`

`metrics` currently includes:

- `latency_ms`
- `input_tokens`
- `output_tokens`

## Useful Commands

One-shot prompt:

```powershell
python skills/codex-ws-client/scripts/codex_ws_client.py "Summarize this repo"
```

JSON output for tool use:

```powershell
python skills/codex-ws-client/scripts/codex_ws_client.py --json "List the main entrypoints"
```

Reuse a persisted thread:

```powershell
python skills/codex-ws-client/scripts/codex_ws_client.py --thread-id THREAD_ID "Continue the previous conversation"
```

Interactive REPL:

```powershell
python skills/codex-ws-client/scripts/codex_ws_client.py --repl --print-thread-id
```

REPL with interactive approvals:

```powershell
python skills/codex-ws-client/scripts/codex_ws_client.py --repl --interactive-approvals
```

Prompt from file:

```powershell
python skills/codex-ws-client/scripts/codex_ws_client.py --prompt-file prompt.txt
```

Structured output with trace:

```powershell
python skills/codex-ws-client/scripts/codex_ws_client.py --json --ndjson-file trace.jsonl "Return metadata"
```

## REPL Commands

Available in REPL mode:

- `/thread` prints the current thread id
- `/new` creates a new thread
- `/exit` or `/quit` exits the REPL

## Logging And Debugging

Verbosity:

- `-v` prints lifecycle and selected notification summaries to stderr
- `-vv` prints raw JSON-RPC traffic to stderr

Trace file:

- `--ndjson-file FILE` appends JSON-RPC traffic as JSON lines

Summary:

- `--summary` prints token usage and latency to stderr

Save final message:

- `--out FILE` writes the final assistant text to a file

## Approval Handling

Default behavior:

- command approvals are auto-declined
- file-change approvals are auto-declined
- permission requests are denied

REPL override:

- `--interactive-approvals` enables prompt-based handling for:
  - command approvals
  - file-change approvals
  - permission requests

Still unsupported:

- dynamic tool execution requested by server
- tool user input requests outside the simple approval prompts
- ChatGPT auth token refresh requests

Unsupported server requests are answered explicitly instead of being ignored.

## Timeouts

`--timeout`

- normal WebSocket message wait timeout

`--connect-timeout`

- initial connection timeout

`--resume-timeout`

- timeout for turns sent on resumed threads

Set any of them to `0` for no timeout.

## Exit Codes

- `0`: success
- `1`: turn failure
- `2`: bad arguments
- `3`: connection failure
- `4`: timeout
- `5`: JSON/schema parse error
- `130`: interrupted

## Best Practices For Another LLM

Prefer:

- `--json` for machine consumption
- `--no-stream` if you only need the final answer text
- `--thread-id` only for known persisted threads
- `--ndjson-file` when debugging protocol behavior

Avoid:

- using `--thread-id` with threads created via `--ephemeral`
- relying on REPL-only features from one-shot mode
- expecting full protocol coverage for every server request type

Recommended one-shot pattern:

```powershell
python skills/codex-ws-client/scripts/codex_ws_client.py --json --connect-timeout 10 --timeout 120 "YOUR PROMPT"
```

Recommended resumed-thread pattern:

```powershell
python skills/codex-ws-client/scripts/codex_ws_client.py --json --thread-id THREAD_ID --resume-timeout 300 "YOUR PROMPT"
```

## Known Limits

- WebSocket only, no stdio mode
- single-process CLI design, not a reusable library
- not a full protocol framework
- Windows graceful interrupt of an in-flight turn is still limited
- richer server-request families are partially handled, not comprehensively implemented

## Relationship To app-server

This script is a client.

It does not start the server automatically.

You must already have something like:

```powershell
codex app-server --listen ws://127.0.0.1:8765
```

running before using it.
