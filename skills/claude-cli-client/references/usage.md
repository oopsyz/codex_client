# claude_cli_client.py Reference

This skill bundles `scripts/claude_cli_client.py`, a lightweight wrapper around the installed `claude` CLI.

## Use cases

Use it when:
- you want a Codex-style wrapper for Claude turns
- you want stable JSON output instead of Claude's raw event stream
- you want session reuse with `--session-id`
- you want a minimal REPL over Claude `--resume`
- you want to launch a long turn in the background with `--detach`

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
- rejected together with `--detach`

Continued session:
- use `--continue` to pick up the most recent conversation in `--cwd`
- mutually exclusive with `--session-id` and `--repl`

Forked session:
- add `--fork-session` alongside `--session-id` to branch instead of reusing the original ID

## Detached turns

`--detach` starts the turn and returns immediately:

```bash
python scripts/claude_cli_client.py --json --detach --detach-log run.jsonl "Long task"
```

Behavior:
- the child is spawned in its own process group (`DETACHED_PROCESS` on Windows, `start_new_session` elsewhere) so it survives this process exiting
- no assistant text is streamed back; the JSON envelope has `status: "detached"`, an empty `text`, and a `pid`
- without `--detach-log` the child's stdout and stderr are discarded
- resume the work later with the printed `session_id`

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

## Pass-through flags

Forwarded to the `claude` CLI unchanged:
- session: `--model`, `--fallback-model`, `--effort`, `--session-name`, `--fork-session`
- prompt: `--system-prompt`, `--append-system-prompt`, `--json-schema`
- agents: `--agent`, `--agents`
- tools: `--tools`, `--allowed-tools`, `--disallowed-tools`, `--add-dir`
- permissions: `--permission-mode` (validated against the CLI's mode list)
- MCP and plugins: `--mcp-config`, `--strict-mcp-config`, `--plugin-dir`, `--plugin-url`
- settings: `--settings`, `--setting-sources`
- budget: `--max-budget-usd`
- misc: `--betas`, `--bare`, `--include-hook-events`, `--exclude-dynamic-system-prompt-sections`, `--disable-slash-commands`

## Known limits

- CLI subprocess transport only
- relies on Claude's installed CLI behavior for session semantics
- does not implement interactive stdin stream-json mode
- `--detach` cannot report turn success; poll the session or read `--detach-log`
