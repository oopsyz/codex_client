# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`codex_client` is a lightweight WebSocket client (packaged as a Claude Code skill) that connects to a running `codex app-server` instance. It enables Claude agents to delegate tasks to Codex via persistent, multi-turn WebSocket sessions using JSON-RPC 2.0.

## Running the Client

The script requires Python 3.9+ and the `websockets` library:

```bash
# Install dependency
pip install websockets

# Basic usage
python skills/codex-ws-client/scripts/codex_ws_client.py "your prompt here"

# With options
python skills/codex-ws-client/scripts/codex_ws_client.py --uri ws://127.0.0.1:8765 --model gpt-5 "prompt"

# Interactive REPL
python skills/codex-ws-client/scripts/codex_ws_client.py --repl

# JSON structured output
python skills/codex-ws-client/scripts/codex_ws_client.py --json "prompt"

# Resume existing thread
python skills/codex-ws-client/scripts/codex_ws_client.py --thread-id <id> "prompt"
```

## Skill Installation

Project-local install (matches README):

```powershell
Copy-Item -Recurse -Force skills/codex-ws-client .codex/skills/codex-ws-client
```

Global install (available across projects):

```powershell
Copy-Item -Recurse -Force skills/codex-ws-client $HOME/.codex/skills/codex-ws-client
```

## Architecture

**Single-file implementation:** All logic lives in `skills/codex-ws-client/scripts/codex_ws_client.py` (~1,300 lines). There are no local module imports.

**Protocol flow:**

1. WebSocket connect to `ws://127.0.0.1:8765` (configurable via `--uri`)
2. JSON-RPC `initialize` + `initialized` handshake
3. `thread/start` (new) or `thread/resume` (existing via `--thread-id`)
4. `turn/start` with the prompt — stream deltas from server
5. Handle server requests (approvals, elicitations) inline; auto-decline by default

**Key async functions:**

- `run_client()` — top-level entry, manages connection lifecycle
- `run_turn()` — sends one prompt, streams response
- `ensure_thread()` — creates or resumes a thread
- `rpc_request()` — generic JSON-RPC call with response matching
- `handle_server_request()` — processes approval/elicitation requests mid-stream

**Output modes:** streaming text (default), buffered (`--no-stream`), structured JSON (`--json`), NDJSON trace log (`--ndjson-file`).

**Exit codes:** 0 success, 1 turn failure, 2 bad args, 3 connection failure, 4 timeout, 5 parse error, 130 SIGINT.

## Key Files

- [skills/codex-ws-client/scripts/codex_ws_client.py](skills/codex-ws-client/scripts/codex_ws_client.py) — full implementation
- [skills/codex-ws-client/SKILL.md](skills/codex-ws-client/SKILL.md) — skill descriptor used by Claude Code
- [skills/codex-ws-client/references/usage.md](skills/codex-ws-client/references/usage.md) — detailed usage reference
- [README.md](README.md) — English documentation
- [README.zh-CN.md](README.zh-CN.md) — Chinese documentation
