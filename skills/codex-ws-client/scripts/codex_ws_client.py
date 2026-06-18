from __future__ import annotations

import ast
import atexit
import argparse
import asyncio
import inspect
import json
import os
import random
import signal
import sys
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any, Awaitable, Callable

import websockets

DEFAULT_URI = "ws://127.0.0.1:8765"
DEFAULT_MODEL = "gpt-5"
APP_SERVER_OVERLOADED = -32001

EXIT_SUCCESS = 0
EXIT_TURN_FAILURE = 1
EXIT_BAD_ARGS = 2
EXIT_CONNECTION_FAILURE = 3
EXIT_TIMEOUT = 4
EXIT_PARSE_ERROR = 5
EXIT_SIGINT = 130


BOM = "﻿"

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


atexit.register(close_ndjson)


def write_ndjson(event_type: str, data: Any, turn_id: str = "") -> None:
    if _ndjson_file is None:
        return
    _ndjson_file.write(json.dumps({"type": event_type, "turn_id": turn_id, "data": data}, ensure_ascii=False) + "\n")
    _ndjson_file.flush()


def safe_print(*args: Any, **kwargs: Any) -> None:
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError as exc:
        raise RuntimeError("Local stdout encoding failed while printing assistant output. Configure stdout for UTF-8.") from exc


def _codex_config_path() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home).expanduser() / "config.toml"
    return Path.home() / ".codex" / "config.toml"


def _strip_toml_comment(line: str) -> str:
    in_single = False
    in_double = False
    escape = False
    for idx, char in enumerate(line):
        if in_double:
            if escape:
                escape = False
                continue
            if char == "\\":
                escape = True
                continue
            if char == '"':
                in_double = False
            continue
        if in_single:
            if char == "'":
                in_single = False
            continue
        if char == "#":
            return line[:idx]
        if char == '"':
            in_double = True
        elif char == "'":
            in_single = True
    return line


def _parse_codex_model_from_config(path: Path) -> str:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return ""

    for raw_line in content.splitlines():
        line = _strip_toml_comment(raw_line).strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            break
        key, sep, value = line.partition("=")
        if key.strip() != "model" or not sep:
            continue
        value = value.strip()
        if not value:
            continue
        if value[:1] in {'"', "'"}:
            try:
                parsed = ast.literal_eval(value)
            except (SyntaxError, ValueError):
                continue
            if isinstance(parsed, str) and parsed.strip():
                return parsed.strip()
            continue
        return value.split()[0]
    return ""


def _find_git_root(start: Path) -> Path | None:
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _iter_project_config_paths(workspace_dir: Path) -> list[Path]:
    current = workspace_dir.resolve()
    root = _find_git_root(current) or current
    lineage: list[Path] = []
    while True:
        lineage.append(current)
        if current == root or current.parent == current:
            break
        current = current.parent
    configs: list[Path] = []
    for candidate in reversed(lineage):
        config = candidate / ".codex" / "config.toml"
        if config.exists():
            configs.append(config)
    return configs


def resolve_default_model(workspace_dir: Path | None = None) -> str:
    config_model = ""
    if workspace_dir is not None:
        for config_path in _iter_project_config_paths(workspace_dir):
            parsed = _parse_codex_model_from_config(config_path)
            if parsed:
                config_model = parsed
    if not config_model:
        config_model = _parse_codex_model_from_config(_codex_config_path())
    return config_model or DEFAULT_MODEL


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


class CancelState:
    def __init__(self) -> None:
        self.active_turn_id: str = ""
        self.cancel_requested: bool = False

    def reset(self) -> None:
        self.active_turn_id = ""
        self.cancel_requested = False


_cancel = CancelState()


class ProtocolParseError(RuntimeError):
    """Raised when the server sends malformed JSON-RPC payloads."""


class RpcError(RuntimeError):
    """Structured JSON-RPC error returned by the app-server."""

    def __init__(self, code: int, message: str, data: Any = None, method: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data
        self.method = method

    @classmethod
    def from_payload(cls, payload: dict[str, Any], method: str = "") -> "RpcError":
        error = payload.get("error") or {}
        return cls(int(error.get("code", -32000)), str(error.get("message", "JSON-RPC request failed")), error.get("data"), method)

    @property
    def error_code(self) -> str:
        return {
            APP_SERVER_OVERLOADED: "app_server_overloaded",
            -32601: "method_not_found",
            -32602: "invalid_params",
            -32603: "internal_error",
        }.get(self.code, f"rpc_{self.code}")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "error_code": self.error_code, "message": self.message}
        if self.method:
            result["method"] = self.method
        if self.data is not None:
            result["data"] = self.data
        return result


@dataclass(frozen=True)
class RetryConfig:
    attempts: int = 4
    base_delay: float = 0.25
    max_delay: float = 4.0
    jitter: float = 0.2


class TurnDeadline:
    """Overall deadline for a turn; per-message timeouts still cap each wait."""

    def __init__(self, seconds: float | None) -> None:
        self.deadline = None if seconds is None or seconds <= 0 else monotonic() + seconds

    def remaining_timeout(self, per_wait_timeout: float | None) -> float | None:
        if self.deadline is None:
            return per_wait_timeout
        remaining = self.deadline - monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError()
        if per_wait_timeout is None:
            return remaining
        return min(per_wait_timeout, remaining)


def is_server_request(message: dict[str, Any]) -> bool:
    return "id" in message and "method" in message and "result" not in message and "error" not in message


def is_notification(message: dict[str, Any]) -> bool:
    return "method" in message and "id" not in message and "result" not in message and "error" not in message


ServerRequestHandler = Callable[[Any, dict[str, Any], int], Awaitable[bool]]


async def _noop_request_handler(_ws: Any, _message: dict[str, Any], _verbosity: int = 0) -> bool:
    return False


class ProtocolClient:
    def __init__(
        self,
        ws: Any,
        *,
        pending_messages: deque[dict[str, Any]] | None = None,
        handle_server_request: ServerRequestHandler = _noop_request_handler,
        verbosity: int = 0,
        retry: RetryConfig = RetryConfig(),
    ) -> None:
        self.ws = ws
        self.pending_messages = pending_messages if pending_messages is not None else deque()
        self.handle_server_request = handle_server_request
        self.verbosity = verbosity
        self.retry = retry

    async def _recv_ws_json(self, timeout: float | None, deadline: TurnDeadline | None = None) -> dict[str, Any]:
        wait_timeout = deadline.remaining_timeout(timeout) if deadline else timeout
        raw = await asyncio.wait_for(self.ws.recv(), timeout=wait_timeout)
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProtocolParseError(f"Failed to parse server message as JSON: {exc}") from exc
        write_ndjson("recv", msg, msg.get("params", {}).get("turnId", ""))
        return msg

    async def recv_json(self, timeout: float | None, deadline: TurnDeadline | None = None) -> dict[str, Any]:
        while True:
            if self.pending_messages:
                msg = self.pending_messages.popleft()
            else:
                msg = await self._recv_ws_json(timeout, deadline)
            if await self.handle_server_request(self.ws, msg, self.verbosity):
                continue
            return msg

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
        deadline: TurnDeadline | None = None,
        retry_overload: bool = True,
    ) -> dict[str, Any]:
        attempts = self.retry.attempts if retry_overload else 1
        for attempt in range(attempts):
            try:
                return await self._request_once(method, params or {}, timeout, deadline)
            except RpcError as exc:
                if exc.code != APP_SERVER_OVERLOADED or attempt >= attempts - 1:
                    raise
                delay = min(self.retry.max_delay, self.retry.base_delay * (2**attempt))
                wait = delay + delay * self.retry.jitter * random.random()
                await asyncio.sleep(deadline.remaining_timeout(wait) if deadline else wait)
        raise RuntimeError("unreachable retry state")

    async def _request_once(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float | None,
        deadline: TurnDeadline | None,
    ) -> dict[str, Any]:
        req_id = str(uuid.uuid4())
        payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        write_ndjson("send", payload)
        await self.ws.send(json.dumps(payload))

        for _ in range(len(self.pending_messages)):
            message = self.pending_messages.popleft()
            if await self.handle_server_request(self.ws, message, self.verbosity):
                continue
            if message.get("id") == req_id:
                if "error" in message:
                    raise RpcError.from_payload(message, method)
                return message.get("result", {})
            self.pending_messages.append(message)

        while True:
            message = await self._recv_ws_json(timeout, deadline)
            if await self.handle_server_request(self.ws, message, self.verbosity):
                continue
            if message.get("id") == req_id:
                if "error" in message:
                    raise RpcError.from_payload(message, method)
                return message.get("result", {})
            self.pending_messages.append(message)

    async def initialize(self, timeout: float | None) -> None:
        await self.request(
            "initialize",
            {"clientInfo": {"name": "codex-ws-client", "title": "Codex WS Client", "version": "0.5"}, "capabilities": {"experimentalApi": True}},
            timeout=timeout,
        )
        await self.ws.send(json.dumps({"jsonrpc": "2.0", "method": "initialized", "params": {}}))

    async def start_thread(self, params: dict[str, Any], timeout: float | None) -> dict[str, Any]:
        return await self.request("thread/start", params, timeout=timeout)

    async def resume_thread(self, params: dict[str, Any], timeout: float | None) -> dict[str, Any]:
        return await self.request("thread/resume", params, timeout=timeout)

    async def interrupt_turn(self, thread_id: str, turn_id: str, timeout: float | None = None) -> dict[str, Any]:
        return await self.request(
            "turn/interrupt",
            {"threadId": thread_id, "turnId": turn_id},
            timeout=timeout,
            retry_overload=False,
        )

    async def unsubscribe_thread(self, thread_id: str, timeout: float | None = None) -> dict[str, Any]:
        return await self.request("thread/unsubscribe", {"threadId": thread_id}, timeout=timeout)

    async def close(self) -> None:
        close = getattr(self.ws, "close", None)
        if close is None:
            return
        result = close()
        if inspect.isawaitable(result):
            await result

    async def read_thread(self, thread_id: str, timeout: float | None, *, include_turns: bool = False) -> dict[str, Any]:
        return await self.request("thread/read", {"threadId": thread_id, "includeTurns": include_turns}, timeout=timeout)

    async def list_threads(self, timeout: float | None, *, cwd: str = "", title: str = "", updated_after: str = "", updated_before: str = "") -> dict[str, Any]:
        params: dict[str, Any] = {"sortKey": "updated_at", "sortDirection": "desc"}
        if cwd:
            params["cwd"] = cwd
        if title:
            params["searchTerm"] = title
        response = await self.request("thread/list", params, timeout=timeout)
        threads = list(response.get("data", []))

        def _as_number(value: Any) -> float | None:
            if isinstance(value, (int, float)):
                return float(value)
            if isinstance(value, str):
                try:
                    return float(value)
                except ValueError:
                    return None
            return None

        def _as_string(value: Any) -> str | None:
            return value if isinstance(value, str) else None

        if updated_after or updated_before:
            filtered: list[dict[str, Any]] = []
            for thread in threads:
                updated_at = thread.get("updatedAt")
                if updated_at is None:
                    continue
                if isinstance(updated_at, (int, float)):
                    lower = _as_number(updated_after)
                    upper = _as_number(updated_before)
                    value = float(updated_at)
                    if lower is not None and value < lower:
                        continue
                    if upper is not None and value > upper:
                        continue
                else:
                    lower = _as_string(updated_after)
                    upper = _as_string(updated_before)
                    value = str(updated_at)
                    if lower is not None and value < lower:
                        continue
                    if upper is not None and value > upper:
                        continue
                filtered.append(thread)
            response["data"] = filtered
        return response


async def default_server_request_handler(ws: Any, message: dict[str, Any], _verbosity: int = 0) -> bool:
    if not is_server_request(message):
        return False
    method = message.get("method", "")
    req_id = message.get("id")
    if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval", "execCommandApproval", "applyPatchApproval"}:
        if _interactive_approvals_enabled:
            print(f"\nApproval requested for {method}.", file=sys.stderr)
            choice = prompt_choice("Approve? [a]ccept/[d]ecline (default d): ", {"a", "d"}, "d")
            result = {"decision": "accept" if choice == "a" else "decline"}
        else:
            result = {"decision": "decline"}
    elif method == "item/permissions/requestApproval":
        if _interactive_approvals_enabled:
            print("\nAdditional permissions requested.", file=sys.stderr)
            choice = prompt_choice("Grant permissions? [g]rant/[d]eny (default d): ", {"g", "d"}, "d")
            result = {"permissions": message.get("params", {}).get("permissions", {}) if choice == "g" else {}, "scope": "turn"}
        else:
            result = {"permissions": {}, "scope": "turn"}
    elif method == "mcpServer/elicitation/request":
        result = {"action": "decline"}
    elif method == "item/tool/call":
        result = {"success": False, "contentItems": []}
    else:
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not supported by this client: {method}"}}))
        return True
    await ws.send(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}))
    return True


def parse_headers(raw_headers: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for raw in raw_headers:
        if ":" not in raw:
            raise ValueError(f"Malformed header: {raw}")
        name, value = raw.split(":", 1)
        if not name.strip():
            raise ValueError(f"Malformed header: {raw}")
        headers[name.strip()] = value.strip()
    return headers


def install_sigint_handler(loop: asyncio.AbstractEventLoop) -> None:
    def _force_exit() -> None:
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

    def windows_handler(*_: object) -> None:
        if _cancel.active_turn_id and not _cancel.cancel_requested:
            _cancel.cancel_requested = True
            print("\nCtrl+C: requesting turn cancel... (press again to force exit)", file=sys.stderr)
            loop.call_soon_threadsafe(lambda: None)
        else:
            _force_exit()

    signal.signal(signal.SIGINT, windows_handler)


def make_thread_params(args: argparse.Namespace, cwd: str | None, developer_instructions: str) -> dict[str, Any]:
    params: dict[str, Any] = {
        "approvalPolicy": "never",
        "sandbox": args.sandbox,
        "model": args.model,
        "personality": args.personality,
        "developerInstructions": developer_instructions,
        "ephemeral": args.ephemeral,
    }
    if cwd is not None:
        params["cwd"] = cwd
    return params


def make_json_result(thread_id: str, turn_id: str, text: str, status: str, error: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"thread_id": thread_id, "turn_id": turn_id, "status": status, "text": text}
    if error is not None:
        result["error"] = error
    return result


def make_turn_params(args: argparse.Namespace, thread_id: str, cwd: str | None, prompt: str) -> dict[str, Any]:
    params: dict[str, Any] = {"threadId": thread_id, "approvalPolicy": "never", "input": [{"type": "text", "text": prompt}]}
    if cwd is not None:
        params["cwd"] = cwd
    if getattr(args, "output_schema", ""):
        params["outputSchema"] = json.loads(args.output_schema)
    return params


def make_detach_result(thread_id: str, turn_id: str, turn_status: str, unsubscribe_status: str) -> dict[str, Any]:
    return {
        "thread_id": thread_id,
        "turn_id": turn_id,
        "status": "detached",
        "turn_status": turn_status,
        "unsubscribe_status": unsubscribe_status,
    }


async def ensure_thread(
    client: ProtocolClient,
    args: argparse.Namespace,
    cwd: str | None,
    timeout: float | None,
    resume_timeout: float | None,
    *,
    force_new: bool = False,
) -> tuple[str, bool]:
    if args.thread_id and not force_new:
        params = make_thread_params(args, cwd, args.instructions or "Answer concisely.")
        params["threadId"] = args.thread_id
        result = await client.resume_thread(params, resume_timeout)
        status = result.get("thread", {}).get("status", {})
        status_type = status.get("type") if isinstance(status, dict) else status
        if status_type in {"systemError", "notLoaded"}:
            raise RpcError(-32000, f"thread/resume returned unusable status: {status_type}", method="thread/resume")
        return args.thread_id, True
    result = await client.start_thread(make_thread_params(args, cwd, args.instructions or "Answer concisely."), timeout)
    thread_id = result["thread"]["id"]
    if args.print_thread_id:
        print(f"THREAD_ID={thread_id}", file=sys.stderr)
    return thread_id, False


async def run_turn(
    client: ProtocolClient,
    args: argparse.Namespace,
    thread_id: str,
    cwd: str | None,
    prompt: str,
    timeout: float | None,
    deadline_seconds: float | None,
) -> tuple[int, dict[str, Any] | None]:
    deadline = TurnDeadline(deadline_seconds)
    started_at = monotonic()
    turn_id = ""

    async def _close_client() -> None:
        close = getattr(client, "close", None)
        if close is None:
            return
        result = close()
        if inspect.isawaitable(result):
            await result

    try:
        turn = await client.request("turn/start", make_turn_params(args, thread_id, cwd, prompt), timeout=timeout, deadline=deadline)
        turn_id = turn["turn"]["id"]
        _cancel.active_turn_id = turn_id
        _cancel.cancel_requested = False
        deltas: list[str] = []
        completed_text = ""
        while True:
            # Ctrl+C only flips the cancel flag; the interrupt RPC is sent on the next loop
            # iteration, so a quiet/stalled recv still waits for a timeout or a server message.
            if _cancel.cancel_requested and _cancel.active_turn_id == turn_id:
                try:
                    await client.interrupt_turn(thread_id, turn_id, timeout=timeout)
                except Exception:
                    pass
                print("\nInterrupting turn...", file=sys.stderr)
                _cancel.cancel_requested = False
            message = await client.recv_json(timeout, deadline)
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
            elif method == "turn/completed" and params.get("turn", {}).get("id") == turn_id:
                status = params.get("turn", {}).get("status", "completed")
                text = "".join(deltas).strip() or completed_text.strip()
                if status == "failed":
                    result = make_json_result(thread_id, turn_id, "", "failed", params.get("turn", {}).get("error", "Unknown turn failure"))
                    await _close_client()
                    return EXIT_TURN_FAILURE, result if args.json else result
                if args.summary:
                    elapsed_ms = int(round((monotonic() - started_at) * 1000))
                    print(f"[summary] latency end2end={elapsed_ms}ms", file=sys.stderr)
                if args.out:
                    Path(args.out).write_text(text, encoding="utf-8")
                if args.json:
                    await _close_client()
                    return EXIT_SUCCESS, make_json_result(thread_id, turn_id, text, status)
                if args.no_stream:
                    safe_print(text)
                elif text:
                    safe_print()
                _cancel.reset()
                await _close_client()
                return EXIT_SUCCESS, None
            elif method == "turn/failed":
                result = make_json_result(thread_id, turn_id, "", "failed", params.get("error", "Unknown turn failure"))
                _cancel.reset()
                await _close_client()
                return EXIT_TURN_FAILURE, result if args.json else result
    except asyncio.CancelledError:
        if turn_id:
            try:
                await client.interrupt_turn(thread_id, turn_id, timeout=timeout)
            except Exception:
                pass
        _cancel.reset()
        await _close_client()
        return EXIT_SIGINT, None


async def run_detached_turn(
    client: ProtocolClient,
    args: argparse.Namespace,
    thread_id: str,
    cwd: str | None,
    prompt: str,
    timeout: float | None,
) -> dict[str, Any]:
    async def _close_client() -> None:
        close = getattr(client, "close", None)
        if close is None:
            return
        result = close()
        if inspect.isawaitable(result):
            await result

    turn = await client.request("turn/start", make_turn_params(args, thread_id, cwd, prompt), timeout=timeout)
    turn_data = turn["turn"]
    turn_id = turn_data["id"]
    turn_status = turn_data.get("status", "unknown")
    unsubscribe = await client.unsubscribe_thread(thread_id, timeout=timeout)
    unsubscribe_status = str(unsubscribe.get("status", "unknown"))
    await _close_client()
    return make_detach_result(thread_id, turn_id, str(turn_status), unsubscribe_status)


async def run_repl(
    client: ProtocolClient,
    args: argparse.Namespace,
    thread_id: str,
    cwd: str | None,
    timeout: float | None,
    resume_timeout: float | None,
    turn_deadline_seconds: float | None,
) -> int:
    current_timeout = resume_timeout if args.thread_id else timeout
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
            thread_id, _ = await ensure_thread(client, args, cwd, timeout, resume_timeout, force_new=True)
            current_timeout = timeout
            print(f"New thread created. THREAD_ID={thread_id}", file=sys.stderr)
            continue
        exit_code, json_result = await run_turn(client, args, thread_id, cwd, prompt, current_timeout, turn_deadline_seconds)
        if json_result is not None and args.json:
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
            raise RuntimeError(f"Cannot read prompt file: {exc}") from exc
        except (UnicodeDecodeError, ValueError) as exc:
            raise ProtocolParseError(f"Prompt file is not valid UTF-8: {exc}") from exc
    return args.prompt or ""


async def run_client(args: argparse.Namespace) -> int:
    global _interactive_approvals_enabled
    workspace_dir = Path(args.cwd).resolve() if getattr(args, "cwd", "") else Path.cwd().resolve()
    if not getattr(args, "model", ""):
        args.model = resolve_default_model(workspace_dir)
    timeout = args.timeout if args.timeout > 0 else None
    connect_timeout = args.connect_timeout if args.connect_timeout > 0 else None
    resume_timeout = args.resume_timeout if args.resume_timeout > 0 else None
    turn_deadline = args.turn_deadline if args.turn_deadline > 0 else None
    cwd = str(Path(args.cwd).resolve()) if getattr(args, "cwd", "") else None
    _interactive_approvals_enabled = bool(getattr(args, "interactive_approvals", False) and getattr(args, "repl", False))
    try:
        headers = parse_headers(args.header or [])
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_BAD_ARGS
    try:
        if args.output_schema:
            json.loads(args.output_schema)
    except json.JSONDecodeError as exc:
        print(f"Invalid --output-schema JSON: {exc}", file=sys.stderr)
        return EXIT_PARSE_ERROR
    try:
        prompt = resolve_prompt(args)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_BAD_ARGS
    except ProtocolParseError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_PARSE_ERROR
    if args.prompt_file and args.prompt:
        print("Cannot use both prompt and --prompt-file.", file=sys.stderr)
        return EXIT_BAD_ARGS
    if args.detach and args.repl:
        print("Cannot use --detach with --repl.", file=sys.stderr)
        return EXIT_BAD_ARGS
    if args.detach and (args.list_threads or args.read_thread):
        print("Cannot use --detach with --list-threads or --read-thread.", file=sys.stderr)
        return EXIT_BAD_ARGS
    if args.detach and args.ephemeral:
        print("Cannot use --detach with --ephemeral because detached work must be persisted for later reads.", file=sys.stderr)
        return EXIT_BAD_ARGS
    if not args.repl and not args.list_threads and not args.read_thread and not prompt.strip():
        print("A prompt is required unless --repl, --list-threads or --read-thread is used.", file=sys.stderr)
        return EXIT_BAD_ARGS

    connect_kwargs: dict[str, Any] = {"max_size": 8_000_000}
    if headers:
        connect_kwargs["additional_headers"] = headers
    if args.ndjson_file:
        open_ndjson(args.ndjson_file)
    loop = asyncio.get_running_loop()
    install_sigint_handler(loop)
    try:
        async with asyncio.timeout(connect_timeout) if connect_timeout else asyncio.timeout(None):
            ws = await websockets.connect(args.uri, **connect_kwargs)
    except asyncio.TimeoutError:
        print(f"Connection timed out after {connect_timeout}s: {args.uri}", file=sys.stderr)
        return EXIT_TIMEOUT
    except Exception as exc:
        print(f"Connection failed: {exc}", file=sys.stderr)
        return EXIT_CONNECTION_FAILURE

    try:
        async with ws:
            client = ProtocolClient(ws, handle_server_request=default_server_request_handler, verbosity=args.verbose)
            await client.initialize(timeout)
            if args.list_threads:
                result = await client.list_threads(timeout, cwd=args.filter_cwd, title=args.filter_title, updated_after=args.updated_after, updated_before=args.updated_before)
                safe_print(json.dumps(result, indent=2))
                return EXIT_SUCCESS
            if args.read_thread:
                result = await client.read_thread(args.read_thread, timeout, include_turns=args.include_turns)
                safe_print(json.dumps(result, indent=2))
                return EXIT_SUCCESS
            thread_id, reused = await ensure_thread(client, args, cwd, timeout, resume_timeout)
            if args.repl:
                return await run_repl(client, args, thread_id, cwd, timeout, resume_timeout, turn_deadline)
            if args.detach:
                result = await run_detached_turn(client, args, thread_id, cwd, prompt, resume_timeout if reused else timeout)
                if args.json:
                    safe_print(json.dumps(result, indent=2))
                else:
                    safe_print(f"THREAD_ID={result['thread_id']}")
                    safe_print(f"TURN_ID={result['turn_id']}")
                    safe_print(f"TURN_STATUS={result['turn_status']}")
                    safe_print(f"UNSUBSCRIBE_STATUS={result['unsubscribe_status']}")
                return EXIT_SUCCESS
            code, json_result = await run_turn(client, args, thread_id, cwd, prompt, resume_timeout if reused else timeout, turn_deadline)
            if json_result is not None:
                safe_print(json.dumps(json_result, indent=2))
            return code
    except asyncio.TimeoutError:
        print("Timed out waiting for server response.", file=sys.stderr)
        return EXIT_TIMEOUT
    except ProtocolParseError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_PARSE_ERROR
    except RuntimeError as exc:
        if "Local stdout encoding failed" in str(exc):
            print(str(exc), file=sys.stderr)
            return EXIT_TURN_FAILURE
        raise
    except RpcError as exc:
        if args.json:
            safe_print(json.dumps({"status": "error", "error": exc.to_dict()}, indent=2))
        else:
            print(f"RPC error [{exc.error_code}]: {exc.message}", file=sys.stderr)
        return EXIT_TURN_FAILURE
    except websockets.exceptions.ConnectionClosed as exc:
        print(f"WebSocket connection lost: {exc}", file=sys.stderr)
        return EXIT_CONNECTION_FAILURE
    finally:
        if args.ndjson_file:
            close_ndjson()


def main() -> int:
    try:
        return asyncio.run(run_client(parse_args()))
    except KeyboardInterrupt:
        return EXIT_SIGINT
    except RuntimeError as exc:
        if str(exc).startswith("Event loop stopped before Future completed."):
            return EXIT_SIGINT
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Codex app-server WebSocket client.")
    parser.add_argument("prompt", nargs="?", default="")
    parser.add_argument("--uri", default=DEFAULT_URI)
    parser.add_argument("--cwd", default="", help="Explicit working directory to send; omitted lets app-server choose its default.")
    parser.add_argument("--model", default="", help="Model to use. If omitted, read ~/.codex/config.toml and fall back to the client default.")
    parser.add_argument("--sandbox", default="read-only")
    parser.add_argument("--personality", default="pragmatic")
    parser.add_argument("--instructions", default="")
    parser.add_argument("--ephemeral", action="store_true")
    parser.add_argument("--thread-id", default="")
    parser.add_argument("--print-thread-id", action="store_true")
    parser.add_argument("--prompt-file", default="")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-stream", action="store_true")
    parser.add_argument("--repl", action="store_true")
    parser.add_argument("--detach", action="store_true", help="Start a turn, unsubscribe from the thread, print IDs, and exit without waiting for completion.")
    parser.add_argument("--interactive-approvals", action="store_true")
    parser.add_argument("--output-schema", default="")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--ndjson-file", default="")
    parser.add_argument("--out", default="")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--connect-timeout", type=float, default=10)
    parser.add_argument("--resume-timeout", type=float, default=300)
    parser.add_argument("--turn-deadline", type=float, default=0, help="Overall turn deadline in seconds. 0 disables it.")
    parser.add_argument("--header", action="append", default=[])
    parser.add_argument("-v", "--verbose", action="count", default=0)
    parser.add_argument("--list-threads", action="store_true", help="Call thread/list and print JSON.")
    parser.add_argument("--read-thread", default="", metavar="THREAD_ID", help="Call thread/read and print JSON.")
    parser.add_argument("--include-turns", action="store_true", help="Include turns for --read-thread.")
    parser.add_argument("--filter-cwd", default="", help="thread/list cwd filter.")
    parser.add_argument("--filter-title", default="", help="thread/list title filter.")
    parser.add_argument("--updated-after", default="", help="thread/list updatedAt lower bound.")
    parser.add_argument("--updated-before", default="", help="thread/list updatedAt upper bound.")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
