from __future__ import annotations

import argparse
import asyncio
import json
import signal
from collections import deque
import sys
import uuid
from pathlib import Path
from time import perf_counter
from typing import Any

import websockets


DEFAULT_URI = "ws://127.0.0.1:8765"
DEFAULT_MODEL = "gpt-5"
DEFAULT_TIMEOUT = 120
DEFAULT_CONNECT_TIMEOUT = 10
DEFAULT_RESUME_TIMEOUT = 300
BOM = "\ufeff"

# Exit codes
EXIT_SUCCESS = 0
EXIT_TURN_FAILURE = 1
EXIT_BAD_ARGS = 2
EXIT_CONNECTION_FAILURE = 3
EXIT_TIMEOUT = 4
EXIT_PARSE_ERROR = 5
EXIT_SIGINT = 130

if hasattr(sys.stdin, "reconfigure"):
    try:
        sys.stdin.reconfigure(encoding="utf-8-sig")
    except Exception:
        pass
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# NDJSON trace file
# ---------------------------------------------------------------------------

_ndjson_file = None
_interactive_approvals_enabled = False


def open_ndjson(path: str) -> None:
    global _ndjson_file
    _ndjson_file = open(path, "a", encoding="utf-8")


def close_ndjson() -> None:
    global _ndjson_file
    if _ndjson_file:
        _ndjson_file.close()
        _ndjson_file = None


def write_ndjson(event_type: str, data: Any, turn_id: str = "") -> None:
    if _ndjson_file is None:
        return
    import time
    record = {
        "type": event_type,
        "time": time.time(),
        "turn_id": turn_id,
        "data": data,
    }
    _ndjson_file.write(json.dumps(record, ensure_ascii=False) + "\n")
    _ndjson_file.flush()


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

def log_event(verbosity: int, msg: str) -> None:
    if verbosity >= 1:
        print(f"[event] {msg}", file=sys.stderr)


def log_rpc(verbosity: int, direction: str, data: Any) -> None:
    if verbosity >= 2:
        print(f"[rpc {direction}] {json.dumps(data, indent=2)}", file=sys.stderr)


def safe_print(*args: Any, **kwargs: Any) -> None:
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError as exc:
        raise RuntimeError(
            "Local stdout encoding failed while printing assistant output. "
            "Configure stdout for UTF-8."
        ) from exc


def prompt_choice(prompt: str, valid: set[str], default: str) -> str:
    while True:
        try:
            raw = input(prompt).strip().lower()
        except EOFError:
            return default
        except KeyboardInterrupt:
            print(file=sys.stderr)
            return default
        if not raw:
            return default
        if raw in valid:
            return raw
        print(f"Enter one of: {', '.join(sorted(valid))}", file=sys.stderr)


def is_server_request(message: dict[str, Any]) -> bool:
    return "id" in message and "method" in message and "result" not in message and "error" not in message


def is_notification(message: dict[str, Any]) -> bool:
    return "method" in message and "id" not in message and "result" not in message and "error" not in message


async def send_rpc_result(ws: Any, req_id: Any, result: dict[str, Any], verbosity: int = 0) -> None:
    payload = {"jsonrpc": "2.0", "id": req_id, "result": result}
    log_rpc(verbosity, ">>", payload)
    write_ndjson("send", payload)
    await ws.send(json.dumps(payload))


async def send_rpc_error(
    ws: Any,
    req_id: Any,
    code: int,
    message: str,
    verbosity: int = 0,
) -> None:
    payload = {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}
    log_rpc(verbosity, ">>", payload)
    write_ndjson("send", payload)
    await ws.send(json.dumps(payload))


async def handle_server_request(ws: Any, message: dict[str, Any], verbosity: int = 0) -> bool:
    if not is_server_request(message):
        return False

    req_id = message.get("id")
    method = message.get("method", "")
    params = message.get("params", {})
    log_event(verbosity, f"Handling server request: {method}")

    if method == "item/commandExecution/requestApproval":
        command = params.get("command") or "<unknown>"
        if _interactive_approvals_enabled:
            print(f"\nApproval requested for command:\n{command}", file=sys.stderr)
            choice = prompt_choice(
                "Approve command? [a]ccept/[s]ession/[d]ecline/[c]ancel (default d): ",
                {"a", "s", "d", "c"},
                "d",
            )
            decision_map = {"a": "accept", "s": "acceptForSession", "d": "decline", "c": "cancel"}
            await send_rpc_result(ws, req_id, {"decision": decision_map[choice]}, verbosity)
        else:
            print(f"Server requested command approval; auto-declining: {command}", file=sys.stderr)
            await send_rpc_result(ws, req_id, {"decision": "decline"}, verbosity)
        return True

    if method == "item/fileChange/requestApproval":
        reason = params.get("reason") or "file change approval requested"
        if _interactive_approvals_enabled:
            print(f"\nApproval requested for file change: {reason}", file=sys.stderr)
            choice = prompt_choice(
                "Approve file change? [a]ccept/[s]ession/[d]ecline/[c]ancel (default d): ",
                {"a", "s", "d", "c"},
                "d",
            )
            decision_map = {"a": "accept", "s": "acceptForSession", "d": "decline", "c": "cancel"}
            await send_rpc_result(ws, req_id, {"decision": decision_map[choice]}, verbosity)
        else:
            print(f"Server requested file-change approval; auto-declining: {reason}", file=sys.stderr)
            await send_rpc_result(ws, req_id, {"decision": "decline"}, verbosity)
        return True

    if method == "item/permissions/requestApproval":
        reason = params.get("reason") or "additional permissions requested"
        if _interactive_approvals_enabled:
            print(f"\nAdditional permissions requested: {reason}", file=sys.stderr)
            print(json.dumps(params.get("permissions", {}), indent=2), file=sys.stderr)
            choice = prompt_choice(
                "Grant permissions? [g]rant turn/[s]ession/[d]eny (default d): ",
                {"g", "s", "d"},
                "d",
            )
            if choice == "d":
                await send_rpc_result(ws, req_id, {"permissions": {}, "scope": "turn"}, verbosity)
            else:
                scope = "session" if choice == "s" else "turn"
                await send_rpc_result(
                    ws,
                    req_id,
                    {"permissions": params.get("permissions", {}), "scope": scope},
                    verbosity,
                )
        else:
            print(f"Server requested extra permissions; denying: {reason}", file=sys.stderr)
            await send_rpc_result(ws, req_id, {"permissions": {}, "scope": "turn"}, verbosity)
        return True

    if method == "execCommandApproval":
        print("Server requested legacy command approval; auto-denying.", file=sys.stderr)
        await send_rpc_result(ws, req_id, {"decision": "denied"}, verbosity)
        return True

    if method == "applyPatchApproval":
        print("Server requested legacy patch approval; auto-denying.", file=sys.stderr)
        await send_rpc_result(ws, req_id, {"decision": "denied"}, verbosity)
        return True

    if method == "item/tool/requestUserInput":
        print("Server requested user input for a tool; unsupported in this client.", file=sys.stderr)
        await send_rpc_error(ws, req_id, -32000, "Interactive user input is not supported by this client.", verbosity)
        return True

    if method == "mcpServer/elicitation/request":
        print("Server requested MCP elicitation; auto-declining.", file=sys.stderr)
        await send_rpc_result(ws, req_id, {"action": "decline"}, verbosity)
        return True

    if method == "item/tool/call":
        print("Server requested dynamic tool execution; unsupported in this client.", file=sys.stderr)
        await send_rpc_result(ws, req_id, {"success": False, "contentItems": []}, verbosity)
        return True

    if method == "account/chatgptAuthTokens/refresh":
        print("Server requested ChatGPT auth token refresh; unsupported in this client.", file=sys.stderr)
        await send_rpc_error(ws, req_id, -32000, "ChatGPT auth token refresh is not supported by this client.", verbosity)
        return True

    print(f"Server request `{method}` is not supported by this client.", file=sys.stderr)
    await send_rpc_error(ws, req_id, -32601, f"Method not supported by this client: {method}", verbosity)
    return True


# ---------------------------------------------------------------------------
# Cancel state for Ctrl+C
# ---------------------------------------------------------------------------

class CancelState:
    def __init__(self) -> None:
        self.active_turn_id: str = ""
        self.cancel_requested: bool = False
        self.ws: Any = None

    def reset(self) -> None:
        self.active_turn_id = ""
        self.cancel_requested = False


_cancel = CancelState()


# ---------------------------------------------------------------------------
# WebSocket helpers
# ---------------------------------------------------------------------------

def parse_headers(raw_headers: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for h in raw_headers:
        if ":" not in h:
            print(f"Malformed header (missing ':'): {h}", file=sys.stderr)
            raise SystemExit(EXIT_BAD_ARGS)
        name, value = h.split(":", 1)
        name = name.strip()
        value = value.strip()
        if not name:
            print(f"Malformed header (empty name): {h}", file=sys.stderr)
            raise SystemExit(EXIT_BAD_ARGS)
        headers[name] = value
    return headers


async def recv_json(
    ws: Any,
    pending_messages: deque[dict[str, Any]],
    timeout: float | None,
    verbosity: int = 0,
) -> dict[str, Any]:
    while True:
        if pending_messages:
            msg = pending_messages.popleft()
            log_rpc(verbosity, "<<pending", msg)
        else:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            msg = json.loads(raw)
            log_rpc(verbosity, "<<", msg)
            write_ndjson("recv", msg, msg.get("params", {}).get("turnId", ""))
        if await handle_server_request(ws, msg, verbosity):
            continue
        return msg


async def rpc_request(
    ws: Any,
    method: str,
    params: dict[str, Any],
    pending_messages: deque[dict[str, Any]],
    timeout: float | None,
    verbosity: int = 0,
) -> dict[str, Any]:
    req_id = str(uuid.uuid4())
    payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
    log_rpc(verbosity, ">>", payload)
    write_ndjson("send", payload)
    await ws.send(json.dumps(payload))

    # Drain pending queue first - a prior call may have buffered our response.
    for _ in range(len(pending_messages)):
        message = pending_messages.popleft()
        if await handle_server_request(ws, message, verbosity):
            continue
        if message.get("id") == req_id:
            log_rpc(verbosity, "<<pending", message)
            if "error" in message:
                raise RuntimeError(json.dumps(message["error"]))
            return message.get("result", {})
        # Not ours — put it back at the end for run_turn/recv_json to consume.
        pending_messages.append(message)

    # Read from wire until we get our response.
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        message = json.loads(raw)
        log_rpc(verbosity, "<<", message)
        write_ndjson("recv", message)
        if await handle_server_request(ws, message, verbosity):
            continue
        if message.get("id") == req_id:
            if "error" in message:
                raise RuntimeError(json.dumps(message["error"]))
            return message.get("result", {})
        pending_messages.append(message)


async def initialize_client(
    ws: Any,
    pending_messages: deque[dict[str, Any]],
    timeout: float | None,
    verbosity: int = 0,
) -> None:
    log_event(verbosity, "Initializing client...")
    await rpc_request(
        ws,
        "initialize",
        {
            "clientInfo": {"name": "send-jsonrpc", "title": "send-jsonrpc CLI", "version": "0.4"},
            "capabilities": {"experimentalApi": True},
        },
        pending_messages,
        timeout,
        verbosity,
    )
    # Per protocol spec, client must send `initialized` notification immediately after
    initialized_msg = {"jsonrpc": "2.0", "method": "initialized", "params": {}}
    log_rpc(verbosity, ">>", initialized_msg)
    write_ndjson("send", initialized_msg)
    await ws.send(json.dumps(initialized_msg))
    log_event(verbosity, "Client initialized.")


async def create_thread(
    ws: Any,
    args: argparse.Namespace,
    cwd: str,
    developer_instructions: str,
    pending_messages: deque[dict[str, Any]],
    timeout: float | None,
    verbosity: int = 0,
) -> str:
    log_event(verbosity, "Creating new thread...")
    thread = await rpc_request(
        ws,
        "thread/start",
        {
            "cwd": cwd,
            "approvalPolicy": "never",
            "sandbox": args.sandbox,
            "model": args.model,
            "personality": args.personality,
            "developerInstructions": developer_instructions,
            "ephemeral": args.ephemeral,
        },
        pending_messages,
        timeout,
        verbosity,
    )
    thread_id = thread["thread"]["id"]
    log_event(verbosity, f"Thread created: {thread_id}")
    return thread_id


async def read_thread_status(
    ws: Any,
    thread_id: str,
    pending_messages: deque[dict[str, Any]],
    timeout: float | None,
    verbosity: int = 0,
) -> str | None:
    result = await rpc_request(
        ws,
        "thread/read",
        {"threadId": thread_id, "includeTurns": False},
        pending_messages,
        timeout,
        verbosity,
    )
    status = result.get("thread", {}).get("status")
    if isinstance(status, dict):
        status_type = status.get("type")
        if isinstance(status_type, str):
            return status_type
    return None


async def resume_thread(
    ws: Any,
    thread_id: str,
    args: argparse.Namespace,
    cwd: str,
    developer_instructions: str,
    pending_messages: deque[dict[str, Any]],
    timeout: float | None,
    verbosity: int = 0,
) -> str | None:
    log_event(verbosity, f"Resuming thread {thread_id}...")
    result = await rpc_request(
        ws,
        "thread/resume",
        {
            "threadId": thread_id,
            "cwd": cwd,
            "approvalPolicy": "never",
            "sandbox": args.sandbox,
            "model": args.model,
            "personality": args.personality,
            "developerInstructions": developer_instructions,
        },
        pending_messages,
        timeout,
        verbosity,
    )
    status = result.get("thread", {}).get("status")
    if isinstance(status, dict):
        status_type = status.get("type")
        if isinstance(status_type, str):
            log_event(verbosity, f"Resumed thread {thread_id} status: {status_type}")
            return status_type
    log_event(verbosity, f"Resumed thread {thread_id} with no reported status type.")
    return None


async def ensure_thread(
    ws: Any,
    args: argparse.Namespace,
    cwd: str,
    developer_instructions: str,
    pending_messages: deque[dict[str, Any]],
    timeout: float | None,
    resume_timeout: float | None,
    verbosity: int = 0,
) -> tuple[str, bool]:
    """Returns (thread_id, reused) where reused=True if the thread was resumed, not freshly created."""
    thread_id = args.thread_id
    if thread_id:
        log_event(verbosity, f"Reusing thread {thread_id}")
        # Go straight to thread/resume — it handles all states including notLoaded.
        # No thread/read preflight; resume is the single source of truth.
        resumed_status = await resume_thread(
            ws,
            thread_id,
            args,
            cwd,
            developer_instructions,
            pending_messages,
            resume_timeout,
            verbosity,
        )
        if resumed_status == "systemError":
            raise RuntimeError(
                f"Reused thread {thread_id} entered systemError during thread/resume. "
                "Start a fresh thread instead of resuming this one."
            )
        if resumed_status == "notLoaded":
            if args.repl:
                print(
                    f"Reused thread {thread_id} could not be loaded by thread/resume. "
                    "Creating a fresh thread for this REPL session instead.",
                    file=sys.stderr,
                )
                thread_id = await create_thread(ws, args, cwd, developer_instructions, pending_messages, timeout, verbosity)
                if args.print_thread_id:
                    print(f"THREAD_ID={thread_id}", file=sys.stderr)
                return thread_id, False
            raise RuntimeError(
                f"Reused thread {thread_id} could not be loaded by thread/resume. "
                "Rerun without --thread-id to start a fresh thread intentionally."
            )
        if resumed_status == "idle" or resumed_status == "active":
            return thread_id, True
        if resumed_status is not None:
            print(
                f"Reused thread {thread_id} reported unexpected status `{resumed_status}` after thread/resume. Proceeding with reuse.",
                file=sys.stderr,
            )
        return thread_id, True
    thread_id = await create_thread(ws, args, cwd, developer_instructions, pending_messages, timeout, verbosity)
    if args.print_thread_id:
        print(f"THREAD_ID={thread_id}", file=sys.stderr)
    return thread_id, False


def make_json_result(
    thread_id: str,
    turn_id: str,
    text: str,
    status: str,
    error: str | None = None,
    notifications: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "thread_id": thread_id,
        "turn_id": turn_id,
        "status": status,
        "text": text,
    }
    if error is not None:
        result["error"] = error
    if notifications is not None:
        result["notifications"] = notifications
    if metrics is not None:
        result["metrics"] = metrics
    return result


def format_tool_event(method: str, params: dict[str, Any]) -> str | None:
    if method == "item/toolCall/started":
        name = params.get("name", "?")
        call_id = params.get("callId", "")
        return f"  [tool] started: {name} (id={call_id})"
    if method == "item/toolCall/delta":
        return None  # too noisy for -v; visible at -vv via log_rpc
    if method == "item/toolCall/completed":
        name = params.get("name", "?")
        call_id = params.get("callId", "")
        return f"  [tool] completed: {name} (id={call_id})"
    if method == "item/created":
        item = params.get("item", {})
        role = item.get("role", "")
        itype = item.get("type", "")
        return f"  [item] created: type={itype} role={role}"
    return None


def format_notification_event(method: str, params: dict[str, Any]) -> str | None:
    if method == "thread/status/changed":
        thread_id = params.get("threadId", "?")
        status = params.get("status", {})
        status_type = status.get("type", "?") if isinstance(status, dict) else status
        active_flags = status.get("activeFlags", []) if isinstance(status, dict) else []
        suffix = f" flags={','.join(active_flags)}" if active_flags else ""
        return f"  [thread] status changed: {thread_id} -> {status_type}{suffix}"
    if method == "thread/started":
        thread = params.get("thread", {})
        return f"  [thread] started: {thread.get('id', '?')}"
    if method == "thread/nameUpdated":
        return f"  [thread] renamed: {params.get('threadId', '?')} -> {params.get('name', '')}"
    if method == "thread/archived":
        return f"  [thread] archived: {params.get('threadId', '?')}"
    if method == "thread/unarchived":
        return f"  [thread] unarchived: {params.get('threadId', '?')}"
    if method == "thread/closed":
        return f"  [thread] closed: {params.get('threadId', '?')}"
    if method == "thread/tokenUsage/updated":
        usage = (params.get("tokenUsage") or {}).get("total") or {}
        input_tokens = usage.get("inputTokens", "?")
        output_tokens = usage.get("outputTokens", "?")
        return f"  [thread] token usage updated: in={input_tokens} out={output_tokens}"
    if method == "item/commandExecution/outputDelta":
        item_id = params.get("itemId", "?")
        stream = params.get("stream", "?")
        delta = params.get("delta", "")
        return f"  [command] output delta: item={item_id} stream={stream} chars={len(delta)}"
    if method == "item/commandExecution/terminalInteraction":
        item_id = params.get("itemId", "?")
        interaction = params.get("interaction", {})
        kind = interaction.get("type", "?") if isinstance(interaction, dict) else "?"
        return f"  [command] terminal interaction: item={item_id} type={kind}"
    if method == "item/fileChange/outputDelta":
        item_id = params.get("itemId", "?")
        delta = params.get("delta", "")
        return f"  [fileChange] output delta: item={item_id} chars={len(delta)}"
    if method == "error":
        return f"  [error] {params.get('message', 'server error notification')}"
    if method == "deprecationNotice":
        return f"  [deprecation] {params.get('message', 'deprecated server behavior')}"
    if method == "configWarning":
        return f"  [config] warning: {params.get('message', '')}"
    return None


async def send_turn_interrupt(ws: Any, thread_id: str, turn_id: str, verbosity: int = 0) -> None:
    """Send turn/interrupt per spec. Requires both threadId and turnId."""
    try:
        req_id = str(uuid.uuid4())
        payload = {"jsonrpc": "2.0", "id": req_id, "method": "turn/interrupt", "params": {"threadId": thread_id, "turnId": turn_id}}
        log_rpc(verbosity, ">>", payload)
        write_ndjson("send", payload, turn_id)
        await ws.send(json.dumps(payload))
        log_event(verbosity, f"Sent turn/interrupt for {turn_id}")
    except Exception:
        pass  # best-effort


async def run_turn(
    ws: Any,
    thread_id: str,
    args: argparse.Namespace,
    cwd: str,
    prompt: str,
    pending_messages: deque[dict[str, Any]],
    timeout: float | None,
    verbosity: int = 0,
) -> tuple[int, dict[str, Any] | None]:
    turn_start_time = perf_counter()

    turn_params: dict[str, Any] = {
        "threadId": thread_id,
        "cwd": cwd,
        "approvalPolicy": "never",
        "input": [{"type": "text", "text": prompt}],
    }
    if args.output_schema:
        turn_params["outputSchema"] = json.loads(args.output_schema)

    log_event(verbosity, "Starting turn...")
    turn = await rpc_request(ws, "turn/start", turn_params, pending_messages, timeout, verbosity)
    turn_id = turn["turn"]["id"]
    log_event(verbosity, f"Turn started: {turn_id}")

    _cancel.active_turn_id = turn_id
    _cancel.cancel_requested = False

    deltas: list[str] = []
    completed_text = ""
    token_usage: dict[str, Any] = {}
    notification_summary: dict[str, Any] = {
        "thread_status_updates": [],
        "command_output": [],
        "command_terminal_interactions": [],
        "file_change_output": [],
        "errors": [],
        "deprecations": [],
        "config_warnings": [],
    }

    while True:
        # Check if Ctrl+C requested cancel
        if _cancel.cancel_requested and _cancel.active_turn_id == turn_id:
            await send_turn_interrupt(ws, thread_id, turn_id, verbosity)
            print("\nInterrupting turn...", file=sys.stderr)
            _cancel.cancel_requested = False
            # Continue pumping events until turn/completed or turn/failed

        try:
            message = await recv_json(ws, pending_messages, timeout, verbosity)
        except asyncio.CancelledError:
            await send_turn_interrupt(ws, thread_id, turn_id, verbosity)
            _cancel.reset()
            return EXIT_SIGINT, None

        method = message.get("method")
        params = message.get("params", {})

        if method == "item/agentMessage/delta" and params.get("turnId") == turn_id:
            delta = params.get("delta", "")
            deltas.append(delta)
            if not args.no_stream and not args.json:
                safe_print(delta, end="", flush=True)
        elif method == "item/completed" and params.get("turnId") == turn_id:
            item = params.get("item", {})
            if item.get("type") == "agentMessage":
                completed_text = item.get("text", "")
        elif method == "thread/tokenUsage/updated":
            # Capture token usage for --summary
            token_usage = params
        elif method == "thread/status/changed":
            status = params.get("status", {})
            notification_summary["thread_status_updates"].append(
                {
                    "thread_id": params.get("threadId"),
                    "status": status.get("type") if isinstance(status, dict) else status,
                    "active_flags": status.get("activeFlags", []) if isinstance(status, dict) else [],
                }
            )
        elif method == "turn/completed":
            turn_payload = params.get("turn", {})
            if turn_payload.get("id") == turn_id:
                turn_status = turn_payload.get("status", "completed")
                if turn_status == "failed":
                    error = turn_payload.get("error") or "Unknown turn failure."
                    log_event(verbosity, f"Turn failed: {error}")
                    _cancel.reset()
                    if args.json:
                        return EXIT_TURN_FAILURE, make_json_result(
                            thread_id,
                            turn_id,
                            "",
                            "failed",
                            str(error),
                            notification_summary,
                            {"latency_ms": int(round((perf_counter() - turn_start_time) * 1000))},
                        )
                    print(f"Turn failed: {error}", file=sys.stderr)
                    return EXIT_TURN_FAILURE, None
                if turn_status == "interrupted":
                    log_event(verbosity, "Turn interrupted.")
                    _cancel.reset()
                    final_text = "".join(deltas).strip() or completed_text.strip()
                    if args.json:
                        return EXIT_SUCCESS, make_json_result(
                            thread_id,
                            turn_id,
                            final_text,
                            "interrupted",
                            notifications=notification_summary,
                            metrics={"latency_ms": int(round((perf_counter() - turn_start_time) * 1000))},
                        )
                    if final_text:
                        if not args.no_stream:
                            safe_print()
                        else:
                            safe_print(final_text)
                    print("Turn was interrupted.", file=sys.stderr)
                    return EXIT_SUCCESS, None
                log_event(verbosity, "Turn completed.")
                break
        elif method == "turn/failed":
            # Belt-and-suspenders: some servers emit turn/failed as a separate
            # notification instead of (or in addition to) turn/completed with
            # status "failed".
            fail_turn = params.get("turn", params)
            if fail_turn.get("id", turn_id) == turn_id:
                error = fail_turn.get("error") or params.get("error") or "Unknown turn failure."
                log_event(verbosity, f"Turn failed: {error}")
                _cancel.reset()
                if args.json:
                    return EXIT_TURN_FAILURE, make_json_result(
                        thread_id,
                        turn_id,
                        "",
                        "failed",
                        str(error),
                        notification_summary,
                        {"latency_ms": int(round((perf_counter() - turn_start_time) * 1000))},
                    )
                print(f"Turn failed: {error}", file=sys.stderr)
                return EXIT_TURN_FAILURE, None
        elif method == "item/commandExecution/outputDelta":
            notification_summary["command_output"].append(
                {
                    "item_id": params.get("itemId"),
                    "stream": params.get("stream"),
                    "chars": len(params.get("delta", "")),
                }
            )
        elif method == "turn/diff/updated":
            if verbosity >= 1:
                diff = params.get("diff", "")
                if diff:
                    print(f"  [diff] updated ({len(diff)} chars)", file=sys.stderr)
        elif method == "item/commandExecution/terminalInteraction":
            interaction = params.get("interaction", {})
            notification_summary["command_terminal_interactions"].append(
                {
                    "item_id": params.get("itemId"),
                    "type": interaction.get("type") if isinstance(interaction, dict) else None,
                }
            )
        elif method == "item/fileChange/outputDelta":
            notification_summary["file_change_output"].append(
                {
                    "item_id": params.get("itemId"),
                    "chars": len(params.get("delta", "")),
                }
            )
        elif method == "turn/plan/updated":
            if verbosity >= 1:
                plan = params.get("plan", [])
                explanation = params.get("explanation", "")
                steps_summary = ", ".join(
                    f"{s.get('step', '?')}({s.get('status', '?')})" for s in plan[:5]
                )
                if explanation:
                    print(f"  [plan] {explanation}: {steps_summary}", file=sys.stderr)
                else:
                    print(f"  [plan] {steps_summary}", file=sys.stderr)
        elif method == "error":
            notification_summary["errors"].append({"message": params.get("message", "")})
        elif method == "deprecationNotice":
            notification_summary["deprecations"].append({"message": params.get("message", "")})
        elif method == "configWarning":
            notification_summary["config_warnings"].append({"message": params.get("message", "")})
        else:
            # Surface richer non-terminal notifications at -v.
            if verbosity >= 1 and is_notification(message):
                notification_line = format_notification_event(method, params)
                if notification_line:
                    print(notification_line, file=sys.stderr)
                    continue
                tool_line = format_tool_event(method, params)
                if tool_line:
                    print(tool_line, file=sys.stderr)
                    continue

    _cancel.reset()
    turn_elapsed_ms = int(round((perf_counter() - turn_start_time) * 1000))

    final_text = "".join(deltas).strip() or completed_text.strip()
    usage = (token_usage.get("tokenUsage") or {}).get("total") or token_usage.get("usage") or {}
    metrics = {
        "latency_ms": turn_elapsed_ms,
        "input_tokens": usage.get("inputTokens", usage.get("input_tokens")),
        "output_tokens": usage.get("outputTokens", usage.get("output_tokens")),
    }

    # --summary: token/latency summary
    if args.summary:
        input_tokens = usage.get("inputTokens", usage.get("input_tokens", "?"))
        output_tokens = usage.get("outputTokens", usage.get("output_tokens", "?"))
        print(
            f"[summary] tokens in={input_tokens} out={output_tokens} | latency end2end={turn_elapsed_ms}ms",
            file=sys.stderr,
        )

    # --out: save assistant text to file
    if args.out:
        try:
            out_path = Path(args.out)
            out_path.write_text(final_text, encoding="utf-8")
            log_event(verbosity, f"Output saved to {args.out}")
        except OSError as exc:
            print(f"Failed to write --out file: {exc}", file=sys.stderr)

    if args.json:
        return EXIT_SUCCESS, make_json_result(
            thread_id,
            turn_id,
            final_text,
            "completed",
            notifications=notification_summary,
            metrics=metrics,
        )

    if args.no_stream:
        safe_print(final_text)
    elif final_text:
        safe_print()
    else:
        print("\nNo assistant text returned.", file=sys.stderr)
        return EXIT_TURN_FAILURE, None
    return EXIT_SUCCESS, None


async def run_repl(
    ws: Any,
    thread_id: str,
    args: argparse.Namespace,
    cwd: str,
    developer_instructions: str,
    pending_messages: deque[dict[str, Any]],
    timeout: float | None,
    resume_timeout: float | None,
    reused_thread: bool,
    verbosity: int = 0,
) -> int:
    current_timeout = resume_timeout if reused_thread else timeout
    print("REPL mode. Type /exit to quit, /thread to print thread ID, /new to start a new thread.", file=sys.stderr)
    print(f"THREAD_ID={thread_id}", file=sys.stderr)
    while True:
        try:
            prompt = input("> ").strip()
        except EOFError:
            print(file=sys.stderr)
            return EXIT_SUCCESS
        except KeyboardInterrupt:
            print("\nInterrupted.", file=sys.stderr)
            return EXIT_SIGINT

        if not prompt:
            continue
        prompt = prompt.lstrip(BOM).strip()
        if not prompt:
            continue
        if prompt in {"/exit", "/quit"}:
            return EXIT_SUCCESS
        if prompt == "/thread":
            print(f"THREAD_ID={thread_id}", file=sys.stderr)
            continue
        if prompt == "/new":
            try:
                thread_id = await create_thread(ws, args, cwd, developer_instructions, pending_messages, timeout, verbosity)
                current_timeout = timeout
                reused_thread = False
                print(f"New thread created. THREAD_ID={thread_id}", file=sys.stderr)
            except Exception as exc:
                print(f"Failed to create new thread: {exc}", file=sys.stderr)
            continue

        try:
            exit_code, json_result = await run_turn(
                ws, thread_id, args, cwd, prompt, pending_messages, current_timeout, verbosity,
            )
        except asyncio.TimeoutError:
            if reused_thread:
                print(
                    f"Timed out waiting for server response on reused thread {thread_id} "
                    f"(timeout={current_timeout}s). The thread may be stale after being idle. "
                    "Use /new to start a fresh thread or increase --resume-timeout.",
                    file=sys.stderr,
                )
            else:
                print(f"Timed out waiting for server response (timeout={current_timeout}s).", file=sys.stderr)
            continue
        if json_result is not None:
            safe_print(json.dumps(json_result, indent=2))
        if exit_code != EXIT_SUCCESS:
            print("Turn failed. REPL session is still active.", file=sys.stderr)


def resolve_prompt(args: argparse.Namespace) -> str:
    if args.prompt_file:
        if args.prompt_file == "-":
            return sys.stdin.read()
        try:
            return Path(args.prompt_file).read_text(encoding="utf-8")
        except PermissionError as exc:
            print(f"Cannot read prompt file: {exc}", file=sys.stderr)
            raise SystemExit(EXIT_BAD_ARGS)
        except (UnicodeDecodeError, ValueError) as exc:
            print(f"Prompt file is not valid UTF-8: {exc}", file=sys.stderr)
            raise SystemExit(EXIT_PARSE_ERROR)
    return args.prompt or ""


def install_sigint_handler(loop: asyncio.AbstractEventLoop) -> None:
    def _force_exit() -> None:
        # Schedule loop stop through the event loop so in-flight coroutines get
        # a chance to unwind via CancelledError before the process exits.
        print("\nForce exit.", file=sys.stderr)
        loop.call_soon_threadsafe(loop.stop)

    def async_handler() -> None:
        if _cancel.active_turn_id and not _cancel.cancel_requested:
            _cancel.cancel_requested = True
            print("\nCtrl+C: requesting turn cancel... (press again to force exit)", file=sys.stderr)
        else:
            _force_exit()

    try:
        loop.add_signal_handler(signal.SIGINT, async_handler)
        return
    except NotImplementedError:
        pass

    # Windows fallback: signal.signal() handler runs on the main thread.
    # Set the cancel flag and wake the event loop; force-exit schedules loop.stop
    # through call_soon_threadsafe to avoid raising SystemExit from signal context.
    def windows_handler(*_: object) -> None:
        if _cancel.active_turn_id and not _cancel.cancel_requested:
            _cancel.cancel_requested = True
            print("\nCtrl+C: requesting turn cancel... (press again to force exit)", file=sys.stderr)
            loop.call_soon_threadsafe(lambda: None)  # wake the event loop
        else:
            _force_exit()

    signal.signal(signal.SIGINT, windows_handler)


async def run_client(args: argparse.Namespace) -> int:
    global _interactive_approvals_enabled
    verbosity = args.verbose
    timeout: float | None = args.timeout if args.timeout > 0 else None
    connect_timeout: float | None = args.connect_timeout if args.connect_timeout > 0 else None
    resume_timeout: float | None = args.resume_timeout if args.resume_timeout > 0 else None
    _interactive_approvals_enabled = bool(args.repl and args.interactive_approvals)

    # Validate --output-schema early
    if args.output_schema:
        try:
            json.loads(args.output_schema)
        except json.JSONDecodeError as exc:
            print(f"Invalid --output-schema JSON: {exc}", file=sys.stderr)
            return EXIT_PARSE_ERROR

    # Validate --prompt-file early
    if args.prompt_file and args.prompt_file != "-":
        p = Path(args.prompt_file)
        if not p.exists():
            print(f"Prompt file not found: {args.prompt_file}", file=sys.stderr)
            return EXIT_BAD_ARGS

    if args.prompt_file and args.prompt:
        print("Cannot use both a positional prompt and --prompt-file. Choose one input source.", file=sys.stderr)
        return EXIT_BAD_ARGS

    # Reject --repl --prompt-file - (stdin would be exhausted before REPL starts)
    if args.repl and args.prompt_file == "-":
        print("Cannot use --prompt-file - with --repl; stdin is needed for interactive input.", file=sys.stderr)
        return EXIT_BAD_ARGS

    if args.interactive_approvals and not args.repl:
        print("--interactive-approvals requires --repl.", file=sys.stderr)
        return EXIT_BAD_ARGS

    # Validate headers early
    extra_headers = parse_headers(args.header) if args.header else {}

    developer_instructions = args.instructions or "Answer concisely."
    cwd = str(Path(args.cwd).resolve())

    prompt = "" if args.repl else resolve_prompt(args)
    if not args.repl and not prompt.strip():
        print("A prompt is required unless --repl is used. Pass a positional argument or --prompt-file.", file=sys.stderr)
        return EXIT_BAD_ARGS

    # Open NDJSON trace file
    if args.ndjson_file:
        try:
            open_ndjson(args.ndjson_file)
        except OSError as exc:
            print(f"Cannot open --ndjson-file: {exc}", file=sys.stderr)
            return EXIT_BAD_ARGS

    # Install Ctrl+C handler
    loop = asyncio.get_running_loop()
    install_sigint_handler(loop)

    log_event(verbosity, f"Connecting to {args.uri}...")
    try:
        connect_kwargs: dict[str, Any] = {"max_size": 8_000_000}
        if extra_headers:
            connect_kwargs["additional_headers"] = extra_headers
        async with asyncio.timeout(connect_timeout) if connect_timeout else asyncio.timeout(None):
            ws = await websockets.connect(args.uri, **connect_kwargs)
    except asyncio.TimeoutError:
        print(f"Connection timed out after {connect_timeout}s: {args.uri}", file=sys.stderr)
        return EXIT_TIMEOUT
    except Exception as exc:
        print(f"Connection failed: {exc}", file=sys.stderr)
        return EXIT_CONNECTION_FAILURE

    _cancel.ws = ws
    reused = False

    try:
        async with ws:
            log_event(verbosity, "Connected.")
            pending_messages: deque[dict[str, Any]] = deque()
            await initialize_client(ws, pending_messages, timeout, verbosity)
            thread_id, reused = await ensure_thread(
                ws,
                args,
                cwd,
                developer_instructions,
                pending_messages,
                timeout,
                resume_timeout,
                verbosity,
            )
            if args.repl:
                return await run_repl(
                    ws,
                    thread_id,
                    args,
                    cwd,
                    developer_instructions,
                    pending_messages,
                    timeout,
                    resume_timeout,
                    reused,
                    verbosity,
                )
            turn_timeout = resume_timeout if reused else timeout
            exit_code, json_result = await run_turn(ws, thread_id, args, cwd, prompt, pending_messages, turn_timeout, verbosity)
            if json_result is not None:
                safe_print(json.dumps(json_result, indent=2))
            return exit_code
    except asyncio.TimeoutError:
        if reused:
            print(
                f"Timed out waiting for server response on reused thread "
                f"(timeout={resume_timeout}s). The thread may be stale after being idle. "
                "Try a fresh thread or increase --resume-timeout.",
                file=sys.stderr,
            )
        else:
            print(f"Timed out waiting for server response (timeout={timeout}s).", file=sys.stderr)
        return EXIT_TIMEOUT
    except websockets.exceptions.ConnectionClosed as exc:
        print(f"WebSocket connection lost: {exc}", file=sys.stderr)
        return EXIT_CONNECTION_FAILURE
    except OSError as exc:
        print(f"Network error: {exc}", file=sys.stderr)
        return EXIT_CONNECTION_FAILURE
    except RuntimeError as exc:
        if "Local stdout encoding failed" in str(exc):
            print(str(exc), file=sys.stderr)
            return EXIT_TURN_FAILURE
        print(f"WebSocket request failed: {exc}", file=sys.stderr)
        return EXIT_TURN_FAILURE
    except Exception as exc:
        print(f"WebSocket request failed: {exc}", file=sys.stderr)
        return EXIT_TURN_FAILURE
    finally:
        close_ndjson()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Send prompts to a running `codex app-server` over WebSocket using the JSON-RPC "
            "methods `initialize`, `thread/start`, and `turn/start`."
        ),
        epilog=(
            "Requirements:\n"
            "  1. `codex app-server` must already be running and listening on `--uri`.\n"
            "  2. This client uses WebSocket transport only; it does not speak stdio.\n"
            "  3. `prompt` is required unless `--repl` is used.\n"
            "  4. `--thread-id` requires the thread was created without `--ephemeral`. Use `/new` or a fresh invocation if resume times out.\n"
            "\n"
            "Exit codes:\n"
            "  0    Success\n"
            "  1    Turn failure\n"
            "  2    Bad arguments\n"
            "  3    Connection failure\n"
            "  4    Timeout\n"
            "  5    JSON/schema parse error\n"
            "  130  Interrupted (SIGINT)\n"
            "\n"
            "Verbosity:\n"
            "  -v   Lifecycle events and tool calls to stderr\n"
            "  -vv  Raw JSON-RPC messages to stderr\n"
            "\n"
            "Examples:\n"
            '  python send_jsonrpc.py "Summarize this repo"\n'
            '  python send_jsonrpc.py --header "Authorization: Bearer tok" "List files"\n'
            '  python send_jsonrpc.py --prompt-file prompt.txt\n'
            '  echo "Explain this" | python send_jsonrpc.py --prompt-file -\n'
            '  python send_jsonrpc.py --json --summary "Return metadata"\n'
            '  python send_jsonrpc.py --ndjson-file trace.jsonl "Debug this"\n'
            '  python send_jsonrpc.py --out response.txt "Write a poem"\n'
            "  python send_jsonrpc.py --repl --print-thread-id\n"
            '  python send_jsonrpc.py --thread-id THREAD_ID "Continue the previous conversation"\n'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "prompt",
        nargs="?",
        default="",
        help="Prompt text sent as the only `turn/start.input` text item. Omit when using `--repl` or `--prompt-file`.",
    )
    parser.add_argument(
        "--uri",
        default=DEFAULT_URI,
        help=f"WebSocket URL for the running Codex app-server. Default: {DEFAULT_URI}",
    )
    parser.add_argument(
        "--cwd",
        default=".",
        help="Working directory to resolve and send in both `thread/start.cwd` and `turn/start.cwd`. Default: current directory.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Model name sent in `thread/start.model` when creating a new thread. Default: {DEFAULT_MODEL}",
    )
    parser.add_argument(
        "--sandbox",
        default="read-only",
        help="Sandbox mode sent in `thread/start.sandbox` when creating a new thread. Default: read-only.",
    )
    parser.add_argument(
        "--personality",
        default="pragmatic",
        help="Personality sent in `thread/start.personality` when creating a new thread. Default: pragmatic.",
    )
    parser.add_argument(
        "--instructions",
        default="",
        help="Developer instructions sent in `thread/start.developerInstructions`. If omitted, defaults to `Answer concisely.`",
    )
    parser.add_argument(
        "--ephemeral",
        action="store_true",
        default=False,
        help="Create an ephemeral thread (not persisted to disk). Ephemeral threads cannot be resumed across connections. Default: false (thread is persisted and resumable).",
    )
    parser.add_argument(
        "--thread-id",
        default="",
        help="Existing thread ID to reuse. Requires the thread was created without --ephemeral.",
    )
    parser.add_argument(
        "--print-thread-id",
        action="store_true",
        help="When a new thread is created, print `THREAD_ID=...` to stderr so it can be reused later.",
    )
    parser.add_argument(
        "--output-schema",
        default="",
        help="Optional JSON string to send as `turn/start.outputSchema` for structured output.",
    )
    parser.add_argument(
        "--no-stream",
        action="store_true",
        help="Do not print delta events as they arrive. Print the final assistant text once after `turn/completed`.",
    )
    parser.add_argument(
        "--repl",
        action="store_true",
        help="Interactive loop that keeps one WebSocket and one thread open for multiple prompts. Supports `/thread`, `/new`, and `/exit`.",
    )
    parser.add_argument(
        "--interactive-approvals",
        action="store_true",
        help="In REPL mode, prompt for approval/file-change/permissions server requests instead of auto-declining them.",
    )
    parser.add_argument(
        "--prompt-file",
        default="",
        help="Read prompt from a file instead of the command line. Use `-` to read from stdin.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output a structured JSON envelope instead of plain text. Includes thread_id, turn_id, status, and text.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"Timeout in seconds for WebSocket message waits. 0 means no timeout. Default: {DEFAULT_TIMEOUT}",
    )
    parser.add_argument(
        "--connect-timeout",
        type=float,
        default=DEFAULT_CONNECT_TIMEOUT,
        help=f"Timeout in seconds for the initial WebSocket connection. 0 means no timeout. Default: {DEFAULT_CONNECT_TIMEOUT}",
    )
    parser.add_argument(
        "--resume-timeout",
        type=float,
        default=DEFAULT_RESUME_TIMEOUT,
        help=f"Timeout in seconds for turns sent on a reused `--thread-id`. 0 means no timeout. Default: {DEFAULT_RESUME_TIMEOUT}",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="count",
        default=0,
        help="Increase verbosity. -v for lifecycle events and tool calls, -vv for raw JSON-RPC messages. Output goes to stderr.",
    )
    parser.add_argument(
        "--header",
        action="append",
        default=[],
        help="Extra HTTP header for the WebSocket handshake. Repeatable. Format: 'Name: Value'.",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print token usage and latency summary to stderr after each turn.",
    )
    parser.add_argument(
        "--ndjson-file",
        default="",
        help="Path to append NDJSON trace records (all JSON-RPC messages with timestamps). For machine-readable debugging.",
    )
    parser.add_argument(
        "--out",
        default="",
        help="Save the final assistant text to a file.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(run_client(parse_args())))
    except (RuntimeError, KeyboardInterrupt):
        # loop.stop() from force-exit handler causes asyncio.run() to raise
        # RuntimeError; KeyboardInterrupt may surface on some platforms.
        raise SystemExit(EXIT_SIGINT)
