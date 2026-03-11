---
name: codex-ws-client
description: Use this skill when working with the bundled `scripts/codex_ws_client.py` WebSocket client for `codex app-server`, including sending prompts, REPL use, resumed threads, JSON output, approval handling, tracing, or debugging Codex app-server protocol behavior.
---

# Codex WS Client

Use the bundled script at `scripts/codex_ws_client.py` as the local client for `codex app-server` over WebSocket.

## Core workflow

1. Confirm the server URI and whether `codex app-server` is already running.
2. Prefer `--json` when another tool or LLM needs machine-readable output.
3. For **multi-turn conversations**: use `--json` and parse `thread_id` from the result to chain turns (see pattern below). Prefer `--repl` when running interactively.
4. Prefer `--thread-id` only for persisted threads created without `--ephemeral`.
5. Use `--ndjson-file` or `-vv` when debugging protocol behavior.

## Script path

The README installs the skill project-locally:

```powershell
Copy-Item -Recurse -Force skills/codex-ws-client .codex/skills/codex-ws-client
```

So the default script path is `.codex/skills/codex-ws-client/scripts/codex_ws_client.py` (relative to project root).

For a global install (`~/.codex/skills/`), replace `.codex/` with `~/.codex/` in all commands below.

## Common commands

These use bash syntax (as executed by Claude agents). For PowerShell, see README.md.

One-shot:

```bash
python .codex/skills/codex-ws-client/scripts/codex_ws_client.py --json "Summarize this repo"
```

Multi-turn (chain via `thread_id` in JSON output — preferred over `--print-thread-id`):

```bash
# Round 1 — capture thread_id from JSON result
result=$(python .codex/skills/codex-ws-client/scripts/codex_ws_client.py --json "First prompt")
thread_id=$(echo "$result" | python -c "import json,sys; print(json.load(sys.stdin)['thread_id'])")

# Round 2+ — reuse thread
python .codex/skills/codex-ws-client/scripts/codex_ws_client.py --json --thread-id "$thread_id" "Follow-up prompt"
```

REPL (preferred for interactive multi-round sessions — single connection, no reconnect overhead):

```bash
python .codex/skills/codex-ws-client/scripts/codex_ws_client.py --repl --interactive-approvals
```

Resume a persisted thread:

```bash
python .codex/skills/codex-ws-client/scripts/codex_ws_client.py --json --thread-id THREAD_ID "Continue the previous conversation"
```

Prompt from file:

```bash
python .codex/skills/codex-ws-client/scripts/codex_ws_client.py --prompt-file prompt.txt
```

Trace protocol traffic:

```bash
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
