---
name: codex-ws-client
description: Use this skill when working with the bundled `scripts/codex_ws_client.py` WebSocket client for `codex app-server`, including sending prompts, REPL use, resumed threads, JSON output, approval handling, tracing, or debugging Codex app-server protocol behavior.
---

# Codex WS Client

Use the bundled script at `scripts/codex_ws_client.py` as the local client for `codex app-server` over WebSocket.

## Core workflow

1. Confirm the server URI and whether `codex app-server` is already running.
2. Prefer `--json` when another tool or LLM needs machine-readable output.
3. Prefer `--thread-id` only for persisted threads created without `--ephemeral`.
4. Use `--repl` for repeated prompts on one connection.
5. Use `--ndjson-file` or `-vv` when debugging protocol behavior.

## Common commands

One-shot:

```powershell
python .codex/skills/codex-ws-client/scripts/codex_ws_client.py --json "Summarize this repo"
```

Resume a persisted thread:

```powershell
python .codex/skills/codex-ws-client/scripts/codex_ws_client.py --json --thread-id THREAD_ID "Continue the previous conversation"
```

REPL:

```powershell
python .codex/skills/codex-ws-client/scripts/codex_ws_client.py --repl --print-thread-id
```

REPL with interactive approvals:

```powershell
python .codex/skills/codex-ws-client/scripts/codex_ws_client.py --repl --interactive-approvals
```

Prompt from file:

```powershell
python .codex/skills/codex-ws-client/scripts/codex_ws_client.py --prompt-file prompt.txt
```

Trace protocol traffic:

```powershell
python .codex/skills/codex-ws-client/scripts/codex_ws_client.py --json --ndjson-file trace.jsonl "Return metadata"
```

## Important behavior

- Transport is WebSocket only.
- The client does not start `codex app-server`; the server must already be running.
- `--ephemeral` threads are not resumable across connections.
- In one-shot mode, stale resumed threads fail fast instead of silently switching context.
- In REPL mode, `/new` starts a fresh thread.
- Approval requests are auto-declined unless `--interactive-approvals` is used in REPL mode.

## Output contract

With `--json`, expect:
- `thread_id`
- `turn_id`
- `status`
- `text`
- optional `error`
- optional `notifications`
- optional `metrics`

`metrics` may include:
- `latency_ms`
- `input_tokens`
- `output_tokens`

## When to load more detail

Read [references/usage.md](references/usage.md) when you need:
- full command patterns
- thread and timeout guidance
- notification/approval behavior
- logging and debugging options
- known limits
