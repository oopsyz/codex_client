# Claude Brainstorm Adapter

`scripts/claude_brainstorm_client.py` is a thin compatibility wrapper around `skills/claude-cli-client/scripts/claude_cli_client.py`.

Use it when you want the brainstorm workflow to treat Claude like a `codex_ws_client`-style peer:

- `--thread-id` maps to Claude `session_id`
- `--print-thread-id` maps to `--print-session-id`
- `--json`, `--repl`, `--no-stream`, `--prompt-file`, `--summary`, `--ndjson-file`, and `--out` pass through

Example:

```bash
result=$(python skills/brainstorm/scripts/claude_brainstorm_client.py --json "Give me your honest, direct take.")
thread_id=$(echo "$result" | python -c "import json,sys; print(json.load(sys.stdin)['thread_id'])")
reply=$(echo "$result" | python -c "import json,sys; print(json.load(sys.stdin)['text'])")
```
