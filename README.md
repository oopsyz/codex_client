# Claude Brainstorm with Codex

[中文说明](README.zh-CN.md)

License: [MIT](LICENSE)

This repository contains `codex_ws_client.py`, a single-file lightweight client for `codex app-server` over WebSocket.

The script lives at `skills/codex-ws-client/scripts/codex_ws_client.py`.

The primary use case is running it inside Claude Code so Claude models can collaborate with Codex through a live `codex app-server` connection.

## Demo




<https://github.com/user-attachments/assets/2c8af159-df13-4d09-ae32-cb2a96eb1fe7>


If inline playback is unavailable, open [brainstorm.mp4](brainstorm.mp4) directly.

It is intended for agents or scripts that need to:

- send a prompt to a running Codex app-server
- reuse a persisted thread with `--thread-id`
- stream or buffer assistant output
- get machine-readable JSON output
- use REPL mode for repeated prompts on one connection
- inspect richer server behavior through stderr logs or NDJSON traces

## Install As A Skill

This repo already packages the client as a skill at `skills/codex-ws-client/`.

To install project-locally (skill available only in this project):

```powershell
Copy-Item -Recurse -Force skills/codex-ws-client .codex/skills/codex-ws-client
```

To install globally (skill available across all projects):

```powershell
Copy-Item -Recurse -Force skills/codex-ws-client $HOME/.codex/skills/codex-ws-client
```

After a project-local install, run the client from that path:

```powershell
python .codex/skills/codex-ws-client/scripts/codex_ws_client.py --json --sandbox read-only "Summarize this repo"
```

After a global install, use `$HOME/.codex/skills/codex-ws-client/scripts/codex_ws_client.py` instead.

Claude CLI sibling client (lets Codex talk to Claude Code, the mirror of this client):

```powershell
Copy-Item -Recurse -Force skills/claude-cli-client .codex/skills/claude-cli-client
python .codex/skills/claude-cli-client/scripts/claude_cli_client.py --json "Summarize this repo"
```

It wraps `claude -p --output-format stream-json` and normalizes the event stream into the
same envelope shape this client emits (`session_id`/`thread_id`, `turn_id`, `status`, `text`),
so both directions of the Codex/Claude bridge share one output contract. It supports
`--session-id` resume, `--repl`, and `--detach` for background turns. See
[skills/claude-cli-client/references/usage.md](skills/claude-cli-client/references/usage.md).

## When To Use It

Use this script when:

- you want Claude Code to delegate work to Codex or continue a shared Codex thread
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

If `--cwd` is omitted, the client leaves `cwd` out of the protocol params and `codex app-server` uses its own default workspace.

When the client runs on Windows against a remote Linux app-server through an
SSH forward, a POSIX-absolute `--cwd` such as `/home/ec2-user/workspace` is
sent unchanged. The client must not rewrite that server-side path into a
local `C:\\home\\...` path.

It handles:

- `item/agentMessage/delta`
- `turn/completed`
- `turn/failed`
- approval/file-change/permissions server requests
- selected thread/tool/command/file-change notifications

## Thread Model

Fresh thread:

- if `--thread-id` is omitted, the client creates a new thread
- exactly one permission selector is required: either
  `--sandbox {read-only,workspace-write,danger-full-access}` or
  `--permissions PROFILE_ID`
- `--sandbox` and `--permissions` cannot be combined
- `danger-full-access` is never selected implicitly

Resumed thread:

- if `--thread-id` is provided, the client calls `thread/resume`
- resumed turns use `--resume-timeout`
- neither `--sandbox` nor `--permissions` can be used with `--thread-id`; the
  existing thread's policy cannot change, so start a fresh thread to choose a
  permission policy

Persistence:

- threads are persisted by default
- `--ephemeral` disables persistence
- `--thread-id` only makes sense for non-ephemeral threads
- `--detach` starts a turn, calls `thread/unsubscribe`, prints IDs, and exits without waiting for completion
- `--unload-thread THREAD_ID` opens the thread, interrupts active turns, cleans background terminals, then requests unsubscription and waits for the server's unload grace period when the server confirms this connection was unsubscribed

Important:

- `--ephemeral` threads cannot be resumed across connections
- `--detach` cannot be used with `--ephemeral` because detached work must be readable later
- detached `status: "detached"` means the client disconnected successfully; inspect the turn later to determine whether the server completed it
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
- effective `sandbox`
- optional `error`
- optional `notifications`
- optional `metrics`

For compatibility, the output field remains named `sandbox`. When a thread is
created with `--permissions`, that field contains the selected profile id.

`metrics` currently includes:

- `latency_ms`
- `input_tokens`
- `output_tokens`

## Useful Commands

One-shot prompt:

```powershell
python skills/codex-ws-client/scripts/codex_ws_client.py --sandbox read-only "Summarize this repo"
```

JSON output for tool use:

```powershell
python skills/codex-ws-client/scripts/codex_ws_client.py --json --sandbox read-only "List the main entrypoints"
```

Reuse a persisted thread:

```powershell
python skills/codex-ws-client/scripts/codex_ws_client.py --thread-id THREAD_ID "Continue the previous conversation"
```

Interactive REPL:

```powershell
python skills/codex-ws-client/scripts/codex_ws_client.py --repl --sandbox read-only --print-thread-id
```

REPL with interactive approvals:

```powershell
python skills/codex-ws-client/scripts/codex_ws_client.py --repl --sandbox read-only --interactive-approvals
```

Prompt from file:

```powershell
python skills/codex-ws-client/scripts/codex_ws_client.py --sandbox read-only --prompt-file prompt.txt
```

Structured output with trace:

```powershell
python skills/codex-ws-client/scripts/codex_ws_client.py --json --sandbox read-only --ndjson-file trace.jsonl "Return metadata"
```

Fire-and-forget long-running work:

```powershell
python skills/codex-ws-client/scripts/codex_ws_client.py --json --sandbox read-only --detach "Run the long task"
```

Without `--json`, `--detach` prints `THREAD_ID=`, `TURN_ID=`, `TURN_STATUS=`, `UNSUBSCRIBE_STATUS=`, and `SANDBOX=` to stdout because those values are the command's primary result.

Check the detached thread later:

```powershell
python skills/codex-ws-client/scripts/codex_ws_client.py --read-thread THREAD_ID --include-turns
```

Stop and unload a thread from this client connection:

```powershell
python skills/codex-ws-client/scripts/codex_ws_client.py --unload-thread THREAD_ID
```

This waits 30 minutes by default after the client unsubscribes, matching the app-server no-subscriber inactivity grace period. The JSON `unload_status` is `thread_closed` only when the server emits that notification; `grace_period_elapsed` means the wait completed but cannot prove this was the last subscriber. Use `--unload-grace-period 0` only when the caller intentionally skips that wait.

Manage background terminals without stopping the app-server:

```powershell
python skills/codex-ws-client/scripts/codex_ws_client.py --list-loaded-threads
python skills/codex-ws-client/scripts/codex_ws_client.py --list-background-terminals THREAD_ID
python skills/codex-ws-client/scripts/codex_ws_client.py --terminate-background-terminal THREAD_ID PROCESS_ID
python skills/codex-ws-client/scripts/codex_ws_client.py --clean-background-terminals THREAD_ID
python skills/codex-ws-client/scripts/codex_ws_client.py --unsubscribe-thread THREAD_ID
```

`PROCESS_ID` is the app-server `processId` returned by `--list-background-terminals`, not an operating-system PID. `--unsubscribe-thread` only affects the invoking connection; use `--unload-thread` when automation needs the complete interrupt, clean, unsubscribe, and grace-period workflow.

Archive a thread after the engine has durably recorded its review bundle:

```powershell
python skills/codex-ws-client/scripts/codex_ws_client.py --archive-thread THREAD_ID
```

The command waits for and returns the server's matching `thread/archived` notification. `--unarchive-thread THREAD_ID` is reserved for explicit operator recovery. `--delete-thread THREAD_ID` permanently removes the server-side thread log and is never used by routine cleanup.

Read one persisted turn in normalized form:

```powershell
python skills/codex-ws-client/scripts/codex_ws_client.py --read-turn THREAD_ID TURN_ID
```

`--read-turn` returns the turn status, concatenated `agentMessage` text, the
raw turn object, and any server error. If the turn is not present, it returns
`status: "not_found"`. This is a transport/read result; callers decide what
the returned text means for their workflow.

Correct an active turn without interrupting it or starting a different turn:

```powershell
python skills/codex-ws-client/scripts/codex_ws_client.py --steer-turn THREAD_ID TURN_ID "Use the corrected scope."
```

This sends `turn/steer` with `TURN_ID` as the app-server's required
`expectedTurnId` precondition. It does not resume the thread or start a new
turn, so a stale ID fails instead of changing a newer active turn.

Wait for a turn to reach a terminal status with normalized JSON output:

```powershell
python skills/codex-ws-client/scripts/codex_ws_client.py --wait-turn THREAD_ID TURN_ID
```

`--wait-turn` polls persisted state without resuming or subscribing to the
thread. The output includes `wait_status` (`terminal` or `timeout`) and
`poll_count`; the default overall wait is 300 seconds (exit code 4 on timeout).
Use `--wait-turn-timeout` and `--wait-turn-poll-interval` to adjust it.

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
- `--detach --json` for long-running turns you want to check later
- `--read-turn THREAD_ID TURN_ID` when a caller needs one turn without parsing the full thread response
- `--no-stream` if you only need the final answer text
- `--thread-id` only for known persisted threads
- `--ndjson-file` when debugging protocol behavior

Avoid:

- using `--thread-id` with threads created via `--ephemeral`
- using `--detach` with `--ephemeral`
- relying on REPL-only features from one-shot mode
- expecting full protocol coverage for every server request type

Recommended one-shot pattern:

```powershell
python skills/codex-ws-client/scripts/codex_ws_client.py --json --sandbox read-only --connect-timeout 10 --timeout 120 "YOUR PROMPT"
```

Recommended resumed-thread pattern:

```powershell
python skills/codex-ws-client/scripts/codex_ws_client.py --json --thread-id THREAD_ID --resume-timeout 300 "YOUR PROMPT"
```

Do not add `--sandbox` to the resumed-thread command. If the sandbox must change, omit `--thread-id` and create a fresh thread with an explicit sandbox.

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

## Issues And Contributions

If you hit a bug, open an issue with the command you ran, the expected behavior, the actual behavior, and any relevant stderr or NDJSON trace output.

Contributions are welcome. Keep changes focused, update documentation when behavior changes, and include validation steps or reproduction notes in the pull request.

