# claude_cli_client.py Reference

This skill bundles `scripts/claude_cli_client.py`, a lightweight wrapper around the installed `claude` CLI.

## Use cases

Use it when:
- you want a Codex-style wrapper for Claude turns
- you want stable JSON output instead of Claude's raw event stream
- you want session reuse with `--session-id`
- you want a minimal REPL over Claude `--resume`

Avoid it when:
- you need a WebSocket transport
- you need every Claude CLI flag exposed exactly as-is
- you need long-lived bidirectional stdin streaming inside one process

## Transport

The wrapper shells out to:

```text
claude -p --verbose --output-format stream-json ...
```

It parses:
- `system`
- `stream_event`
- `assistant`
- `result`
- `rate_limit_event`

## Session behavior

Fresh session:
- omit `--session-id`
- the wrapper generates a new UUID and passes it via `--session-id`

Resumed session:
- provide `--session-id`
- the wrapper calls Claude with `--resume SESSION_ID`
- resumed turns use `--resume-timeout`

Non-persistent session:
- use `--no-session-persistence`
- cannot be resumed later

## REPL behavior

Commands:
- `/session`
- `/new`
- `/exit`
- `/quit`

Implementation note:
- REPL reuses the session by spawning a fresh `claude -p --resume ...` process per prompt

## Logging

- `-v`: lifecycle summaries
- `-vv`: raw streamed JSON lines
- `--ndjson-file FILE`: structured command/stdout/stderr trace
- `--summary`: stderr token, latency, and cost summary
- `--out FILE`: save final assistant text

## JSON result

`--json` emits a structured object with:
- `session_id`
- `thread_id` compatibility alias
- `turn_id`
- `status`
- `text`
- optional notifications and metrics

Metrics may include:
- `latency_ms`
- `input_tokens`
- `output_tokens`
- `cache_read_input_tokens`
- `cache_creation_input_tokens`
- `cost_usd`

## Known limits

- CLI subprocess transport only
- relies on Claude's installed CLI behavior for session semantics
- does not implement interactive stdin stream-json mode
