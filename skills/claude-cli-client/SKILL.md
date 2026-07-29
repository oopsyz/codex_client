---
name: claude-cli-client
description: Use this skill when working with the bundled `scripts/claude_cli_client.py` wrapper for the local `claude` CLI, including one-shot prompts, resumed sessions, REPL use, detached background turns, structured JSON output, or trace/debugging of Claude stream-json output.
---

# Claude CLI Client

Use the bundled script at `scripts/claude_cli_client.py` as a local client for the installed `claude` CLI.

## Core workflow

1. Confirm the `claude` CLI is installed and authenticated.
2. Prefer `--json` when another tool or LLM needs machine-readable output.
3. For multi-turn conversations, parse `session_id` from the JSON output and reuse it with `--session-id`.
4. Prefer `--repl` for interactive sessions.
5. Use `--detach` for long-running work you do not want to block on, then resume with the printed session ID.
6. Use `--ndjson-file` or `-vv` when debugging Claude stream-json behavior.

## Script path

For a project-local install:

```powershell
Copy-Item -Recurse -Force skills/claude-cli-client .codex/skills/claude-cli-client
```

Run it from:

```powershell
python .codex/skills/claude-cli-client/scripts/claude_cli_client.py --json "Summarize this repo"
```

## Common commands

One-shot:

```bash
python .codex/skills/claude-cli-client/scripts/claude_cli_client.py --json "Summarize this repo"
```

Multi-turn:

```bash
result=$(python .codex/skills/claude-cli-client/scripts/claude_cli_client.py --json "First prompt")
session_id=$(echo "$result" | python -c "import json,sys; print(json.load(sys.stdin)['session_id'])")
python .codex/skills/claude-cli-client/scripts/claude_cli_client.py --json --session-id "$session_id" "Follow-up prompt"
```

REPL:

```bash
python .codex/skills/claude-cli-client/scripts/claude_cli_client.py --repl
```

Fire-and-forget long-running work:

```bash
python .codex/skills/claude-cli-client/scripts/claude_cli_client.py --json --detach \
  --detach-log run.jsonl "Refactor the whole test suite"
```

Trace protocol output:

```bash
python .codex/skills/claude-cli-client/scripts/claude_cli_client.py --json --ndjson-file trace.jsonl "Return metadata"
```

## Important behavior

- Transport is local CLI subprocess, not WebSocket.
- Session reuse maps to Claude `session_id` plus `--resume`.
- The wrapper normalizes Claude stream-json into a stable result envelope.
- `--no-session-persistence` disables future resume, so it cannot be combined with `--detach`.
- In REPL mode, `/new` starts a fresh session.
- `--detach` spawns the CLI in its own process group and returns `status: "detached"` with a `pid`. Nothing is streamed back; pass `--detach-log` to capture the child's output.
- `--continue` resumes the most recent conversation in `--cwd` and cannot be combined with `--session-id` or `--repl`.

## Output contract

With `--json`, expect:
- `session_id`
- `thread_id` (compatibility alias of `session_id`)
- `turn_id`
- `status`
- `text`
- optional `error`
- optional `notifications`
- optional `metrics`
