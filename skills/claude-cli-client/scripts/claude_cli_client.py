from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from time import perf_counter
from typing import Any


DEFAULT_CLAUDE_BIN = "claude"
DEFAULT_MODEL = ""
DEFAULT_PERMISSION_MODE = "default"
DEFAULT_RESUME_TIMEOUT = 300.0
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


_ndjson_file = None


def open_ndjson(path: str) -> None:
    global _ndjson_file
    _ndjson_file = open(path, "a", encoding="utf-8")


def close_ndjson() -> None:
    global _ndjson_file
    if _ndjson_file:
        _ndjson_file.close()
        _ndjson_file = None


def write_ndjson(event_type: str, data: Any) -> None:
    if _ndjson_file is None:
        return
    record = {"type": event_type, "data": data}
    _ndjson_file.write(json.dumps(record, ensure_ascii=False) + "\n")
    _ndjson_file.flush()


def safe_print(*args: Any, **kwargs: Any) -> None:
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError as exc:
        raise RuntimeError(
            "Local stdout encoding failed while printing assistant output. "
            "Configure stdout for UTF-8."
        ) from exc


def log_event(verbosity: int, msg: str) -> None:
    if verbosity >= 1:
        print(f"[event] {msg}", file=sys.stderr)


def log_line(verbosity: int, direction: str, line: str) -> None:
    if verbosity >= 2:
        print(f"[{direction}] {line}", file=sys.stderr)





def resolve_prompt(args: argparse.Namespace) -> str:
    if args.prompt_file:
        if args.prompt:
            raise ValueError("Pass either a positional prompt or --prompt-file, not both.")
        if args.prompt_file == "-":
            return sys.stdin.read().lstrip(BOM).strip()
        return Path(args.prompt_file).read_text(encoding="utf-8-sig").lstrip(BOM).strip()
    return args.prompt.lstrip(BOM).strip()


def append_repeatable_args(command: list[str], flag: str, values: list[str]) -> None:
    for value in values:
        command.extend([flag, value])


def resolve_claude_bin(raw_value: str) -> str:
    candidates = [raw_value]
    if sys.platform.startswith("win") and raw_value == DEFAULT_CLAUDE_BIN:
        candidates = ["claude.cmd", "claude.exe", raw_value]
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return raw_value


def build_claude_command(
    args: argparse.Namespace,
    cwd: str,
    prompt: str,
    session_id: str,
    resume: bool,
) -> list[str]:
    command = [
        resolve_claude_bin(args.claude_bin),
        "-p",
        "--verbose",
        "--output-format",
        "stream-json",
    ]
    if not args.no_stream:
        command.append("--include-partial-messages")
    if resume:
        command.extend(["--resume", session_id])
    else:
        command.extend(["--session-id", session_id])
    if args.model:
        command.extend(["--model", args.model])
    if args.permission_mode:
        command.extend(["--permission-mode", args.permission_mode])
    if args.system_prompt:
        command.extend(["--system-prompt", args.system_prompt])
    if args.append_system_prompt:
        command.extend(["--append-system-prompt", args.append_system_prompt])
    if args.json_schema:
        command.extend(["--json-schema", args.json_schema])
    if args.max_budget_usd:
        command.extend(["--max-budget-usd", str(args.max_budget_usd)])
    if args.agent:
        command.extend(["--agent", args.agent])
    if args.effort:
        command.extend(["--effort", args.effort])
    if args.fallback_model:
        command.extend(["--fallback-model", args.fallback_model])
    if args.no_session_persistence:
        command.append("--no-session-persistence")
    if args.disable_slash_commands:
        command.append("--disable-slash-commands")
    append_repeatable_args(command, "--add-dir", args.add_dir)
    append_repeatable_args(command, "--allowed-tools", args.allowed_tools)
    append_repeatable_args(command, "--disallowed-tools", args.disallowed_tools)
    append_repeatable_args(command, "--mcp-config", args.mcp_config)
    append_repeatable_args(command, "--plugin-dir", args.plugin_dir)
    append_repeatable_args(command, "--settings", args.settings)
    append_repeatable_args(command, "--setting-sources", args.setting_sources)
    command.append(prompt)
    return command


def extract_assistant_text(message: dict[str, Any]) -> str:
    content = message.get("content", [])
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(item.get("text", ""))
    return "".join(parts)


def make_json_result(
    session_id: str,
    turn_id: str,
    text: str,
    status: str,
    error: str = "",
    notifications: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "thread_id": session_id,
        "session_id": session_id,
        "turn_id": turn_id,
        "status": status,
        "text": text,
    }
    if error:
        result["error"] = error
    if notifications:
        result["notifications"] = notifications
    if metrics:
        result["metrics"] = metrics
    return result


def summarize_event(line: dict[str, Any]) -> str | None:
    kind = line.get("type")
    if kind == "system":
        subtype = line.get("subtype", "")
        session_id = line.get("session_id", "")
        model = line.get("model", "")
        return f"[system] {subtype} session={session_id} model={model}".strip()
    if kind == "result":
        subtype = line.get("subtype", "")
        duration_ms = line.get("duration_ms", "?")
        return f"[result] {subtype} duration={duration_ms}ms"
    if kind == "rate_limit_event":
        info = line.get("rate_limit_info", {})
        return f"[rate-limit] status={info.get('status', '?')} type={info.get('rateLimitType', '?')}"
    if kind == "assistant":
        return "[assistant] final message received"
    if kind == "stream_event":
        event = line.get("event", {})
        event_type = event.get("type", "")
        if event_type == "message_start":
            model = ((event.get("message") or {}).get("model")) or ""
            return f"[stream] message_start model={model}".strip()
        if event_type == "message_stop":
            return "[stream] message_stop"
    return None


def maybe_handle_permission_denial(
    line: dict[str, Any],
    notifications: dict[str, Any],
    verbosity: int,
) -> None:
    if line.get("type") != "result":
        return
    denials = line.get("permission_denials") or []
    if denials:
        notifications["permission_denials"].extend(denials)
        if verbosity >= 1:
            print(f"[permissions] denials={len(denials)}", file=sys.stderr)


def run_turn(
    args: argparse.Namespace,
    cwd: str,
    prompt: str,
    session_id: str,
    resume: bool,
    timeout: float | None,
    verbosity: int,
) -> tuple[int, dict[str, Any] | None, str]:
    command = build_claude_command(args, cwd, prompt, session_id, resume)
    log_event(verbosity, f"Starting Claude turn with session {session_id}")
    log_line(verbosity, "cmd", json.dumps(command))
    write_ndjson("command", command)

    turn_start_time = perf_counter()
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as exc:
        print(f"Failed to start Claude CLI: {exc}", file=sys.stderr)
        return EXIT_CONNECTION_FAILURE, None, session_id

    partials: list[str] = []
    final_text = ""
    result_payload: dict[str, Any] | None = None
    turn_id = ""
    session_seen = session_id
    assistant_message_id = ""
    notifications: dict[str, Any] = {
        "permission_denials": [],
        "rate_limit_events": [],
        "system": [],
    }

    try:
        assert process.stdout is not None
        while True:
            if timeout is not None and (perf_counter() - turn_start_time) > timeout:
                process.kill()
                print(f"Timed out waiting for Claude CLI output (timeout={timeout}s).", file=sys.stderr)
                return EXIT_TIMEOUT, None, session_seen

            line = process.stdout.readline()
            if not line:
                if process.poll() is not None:
                    break
                continue
            line = line.rstrip("\r\n")
            if not line:
                continue
            log_line(verbosity, "stdout", line)
            write_ndjson("stdout", line)

            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                process.kill()
                print(f"Failed to parse Claude stream-json line: {exc}", file=sys.stderr)
                return EXIT_PARSE_ERROR, None, session_seen

            summary = summarize_event(event)
            if summary and verbosity >= 1:
                print(summary, file=sys.stderr)

            if event.get("session_id"):
                session_seen = str(event["session_id"])

            event_type = event.get("type")
            if event_type == "system":
                notifications["system"].append(
                    {
                        "subtype": event.get("subtype"),
                        "model": event.get("model"),
                        "cwd": event.get("cwd"),
                    }
                )
            elif event_type == "assistant":
                message = event.get("message", {})
                assistant_message_id = message.get("id", assistant_message_id)
                final_text = extract_assistant_text(message)
            elif event_type == "rate_limit_event":
                notifications["rate_limit_events"].append(event.get("rate_limit_info", {}))
            elif event_type == "result":
                result_payload = event
                maybe_handle_permission_denial(event, notifications, verbosity)
            elif event_type == "stream_event":
                stream_event = event.get("event", {})
                stream_type = stream_event.get("type")
                if stream_type == "message_start":
                    message = stream_event.get("message", {})
                    assistant_message_id = message.get("id", assistant_message_id)
                    turn_id = message.get("id", turn_id)
                elif stream_type == "content_block_delta":
                    delta = stream_event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        partials.append(text)
                        if not args.no_stream and not args.json:
                            safe_print(text, end="", flush=True)
                elif stream_type == "message_delta":
                    maybe_usage = stream_event.get("usage", {})
                    if isinstance(maybe_usage, dict):
                        result_payload = result_payload or {}
                        result_payload.setdefault("usage", {}).update(maybe_usage)
                        context_management = stream_event.get("context_management")
                        if context_management is not None:
                            result_payload["context_management"] = context_management

        stderr_text = ""
        if process.stderr is not None:
            stderr_text = process.stderr.read()
            if stderr_text:
                write_ndjson("stderr", stderr_text)
                if verbosity >= 1:
                    print(stderr_text, file=sys.stderr, end="" if stderr_text.endswith("\n") else "\n")

        return_code = process.wait()
        if return_code != 0:
            message = stderr_text.strip() or "Claude CLI exited with a non-zero status."
            print(message, file=sys.stderr)
            if args.json:
                return (
                    EXIT_TURN_FAILURE,
                    make_json_result(
                        session_seen,
                        turn_id or assistant_message_id,
                        final_text or "".join(partials).strip(),
                        "failed",
                        error=message,
                        notifications=notifications,
                        metrics={"latency_ms": int(round((perf_counter() - turn_start_time) * 1000))},
                    ),
                    session_seen,
                )
            return EXIT_TURN_FAILURE, None, session_seen
    except KeyboardInterrupt:
        process.kill()
        print("\nInterrupted.", file=sys.stderr)
        return EXIT_SIGINT, None, session_seen

    completed_text = "".join(partials).strip() or final_text or (result_payload or {}).get("result", "")
    if not turn_id:
        turn_id = assistant_message_id or str(uuid.uuid4())

    elapsed_ms = int(round((perf_counter() - turn_start_time) * 1000))
    usage = (result_payload or {}).get("usage", {})
    metrics = {
        "latency_ms": elapsed_ms,
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
        "cost_usd": (result_payload or {}).get("total_cost_usd"),
    }
    if "context_management" in (result_payload or {}):
        notifications["context_management"] = (result_payload or {}).get("context_management")

    if args.summary:
        print(
            "[summary] "
            f"tokens in={metrics['input_tokens']} out={metrics['output_tokens']} "
            f"| latency end2end={elapsed_ms}ms | cost=${metrics['cost_usd']}",
            file=sys.stderr,
        )

    if args.out:
        try:
            Path(args.out).write_text(completed_text, encoding="utf-8")
            log_event(verbosity, f"Output saved to {args.out}")
        except OSError as exc:
            print(f"Failed to write --out file: {exc}", file=sys.stderr)

    if args.json:
        status = "completed"
        if (result_payload or {}).get("subtype") not in {None, "success"}:
            status = str((result_payload or {}).get("subtype"))
        return (
            EXIT_SUCCESS,
            make_json_result(
                session_seen,
                turn_id,
                completed_text,
                status=status,
                notifications=notifications,
                metrics=metrics,
            ),
            session_seen,
        )

    if args.no_stream:
        safe_print(completed_text)
    elif completed_text:
        safe_print()
    else:
        print("\nNo assistant text returned.", file=sys.stderr)
        return EXIT_TURN_FAILURE, None, session_seen
    return EXIT_SUCCESS, None, session_seen


def run_repl(
    args: argparse.Namespace,
    cwd: str,
    initial_session_id: str,
    verbosity: int,
) -> int:
    session_id = initial_session_id
    reused_session = bool(args.session_id)
    print("REPL mode. Type /exit to quit, /session to print session ID, /new to start a new session.", file=sys.stderr)
    print(f"SESSION_ID={session_id}", file=sys.stderr)

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
        if prompt == "/session":
            print(f"SESSION_ID={session_id}", file=sys.stderr)
            continue
        if prompt == "/new":
            session_id = str(uuid.uuid4())
            reused_session = False
            print(f"New session created. SESSION_ID={session_id}", file=sys.stderr)
            continue

        timeout = args.resume_timeout if reused_session else args.timeout
        exit_code, json_result, session_id = run_turn(
            args=args,
            cwd=cwd,
            prompt=prompt,
            session_id=session_id,
            resume=reused_session,
            timeout=timeout,
            verbosity=verbosity,
        )
        reused_session = True
        if json_result is not None:
            safe_print(json.dumps(json_result, indent=2))
        if exit_code == EXIT_TIMEOUT:
            continue
        if exit_code != EXIT_SUCCESS:
            return exit_code


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Send prompts to the local `claude` CLI using `--print` and parse its "
            "`stream-json` output into a stable client envelope."
        ),
        epilog=(
            "Requirements:\n"
            "  1. The `claude` CLI must already be installed and authenticated.\n"
            "  2. This client wraps the Claude CLI; it is not a WebSocket transport.\n"
            "  3. `prompt` is required unless `--repl` is used.\n"
            "  4. `--session-id` reuses the same Claude conversation via `--resume`.\n"
            "\n"
            "Exit codes:\n"
            "  0    Success\n"
            "  1    Turn failure\n"
            "  2    Bad arguments\n"
            "  3    Claude CLI launch failure\n"
            "  4    Timeout\n"
            "  5    JSON parse error\n"
            "  130  Interrupted (SIGINT)\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("prompt", nargs="?", default="", help="Prompt text to send to Claude.")
    parser.add_argument("--prompt-file", default="", help="Read prompt from a file instead of the command line. Use `-` to read from stdin.")
    parser.add_argument("--claude-bin", default=DEFAULT_CLAUDE_BIN, help=f"Path to the Claude CLI binary. Default: {DEFAULT_CLAUDE_BIN}")
    parser.add_argument("--cwd", default=".", help="Working directory used when spawning the Claude CLI. Default: current directory.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Optional Claude model alias or full name.")
    parser.add_argument("--permission-mode", default=DEFAULT_PERMISSION_MODE, help=f"Claude permission mode. Default: {DEFAULT_PERMISSION_MODE}")
    parser.add_argument("--system-prompt", default="", help="Override the Claude system prompt.")
    parser.add_argument("--append-system-prompt", default="", help="Append additional system prompt text.")
    parser.add_argument("--agent", default="", help="Claude agent to use for the session.")
    parser.add_argument("--effort", default="", help="Claude effort level: low, medium, high, or max.")
    parser.add_argument("--fallback-model", default="", help="Fallback model to use with the Claude CLI.")
    parser.add_argument("--json-schema", default="", help="Optional JSON schema string passed through to `claude --json-schema`.")
    parser.add_argument("--session-id", default="", help="Existing Claude session ID to resume. If omitted, a new UUID is generated.")
    parser.add_argument("--print-session-id", action="store_true", help="Print `SESSION_ID=...` to stderr when a session is created or reused.")
    parser.add_argument("--no-session-persistence", action="store_true", help="Disable Claude session persistence for fresh sessions.")
    parser.add_argument("--no-stream", action="store_true", help="Do not print partial deltas. Print the final assistant text once at end of turn.")
    parser.add_argument("--repl", action="store_true", help="Interactive loop that reuses a Claude session via `--resume`. Supports `/session`, `/new`, and `/exit`.")
    parser.add_argument("--json", action="store_true", help="Output a structured JSON envelope instead of plain text. Includes session_id, turn_id, status, and text.")
    parser.add_argument("--timeout", type=float, default=0, help="Timeout in seconds for fresh sessions. 0 means no timeout.")
    parser.add_argument("--resume-timeout", type=float, default=DEFAULT_RESUME_TIMEOUT, help=f"Timeout in seconds for resumed sessions. 0 means no timeout. Default: {DEFAULT_RESUME_TIMEOUT}")
    parser.add_argument("--max-budget-usd", type=float, default=0.0, help="Optional max budget passed to the Claude CLI.")
    parser.add_argument("--disable-slash-commands", action="store_true", help="Disable Claude slash commands and skills for the spawned session.")
    parser.add_argument("--add-dir", action="append", default=[], help="Repeatable additional directory to allow Claude tool access to.")
    parser.add_argument("--allowed-tools", action="append", default=[], help="Repeatable allowed-tools value passed through to the Claude CLI.")
    parser.add_argument("--disallowed-tools", action="append", default=[], help="Repeatable disallowed-tools value passed through to the Claude CLI.")
    parser.add_argument("--mcp-config", action="append", default=[], help="Repeatable MCP config path or JSON string passed through to the Claude CLI.")
    parser.add_argument("--plugin-dir", action="append", default=[], help="Repeatable Claude plugin directory.")
    parser.add_argument("--settings", action="append", default=[], help="Repeatable Claude settings file or JSON string.")
    parser.add_argument("--setting-sources", action="append", default=[], help="Repeatable setting-sources value passed through to the Claude CLI.")
    parser.add_argument("--summary", action="store_true", help="Print token, latency, and cost summary to stderr after each turn.")
    parser.add_argument("--ndjson-file", default="", help="Path to append NDJSON trace records (command, stdout, stderr).")
    parser.add_argument("--out", default="", help="Save the final assistant text to a file.")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="Increase verbosity. -v for lifecycle events, -vv for raw streamed lines.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cwd = str(Path(args.cwd).resolve())
    timeout = None if args.timeout == 0 else args.timeout
    verbosity = args.verbose

    if args.repl and args.prompt_file == "-":
        print("Cannot use --prompt-file - with --repl; stdin is needed for interactive input.", file=sys.stderr)
        return EXIT_BAD_ARGS

    try:
        prompt = resolve_prompt(args)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_BAD_ARGS

    if not args.repl and not prompt:
        print("A prompt is required unless --repl is used. Pass a positional argument or --prompt-file.", file=sys.stderr)
        return EXIT_BAD_ARGS

    if args.no_session_persistence and args.session_id:
        print("--no-session-persistence cannot be combined with --session-id.", file=sys.stderr)
        return EXIT_BAD_ARGS

    session_id = args.session_id or str(uuid.uuid4())
    if args.print_session_id:
        print(f"SESSION_ID={session_id}", file=sys.stderr)

    if args.ndjson_file:
        open_ndjson(args.ndjson_file)

    try:
        if args.repl:
            return run_repl(args=args, cwd=cwd, initial_session_id=session_id, verbosity=verbosity)

        turn_timeout = (None if args.resume_timeout == 0 else args.resume_timeout) if args.session_id else timeout
        exit_code, json_result, _ = run_turn(
            args=args,
            cwd=cwd,
            prompt=prompt,
            session_id=session_id,
            resume=bool(args.session_id),
            timeout=turn_timeout,
            verbosity=verbosity,
        )
        if json_result is not None:
            safe_print(json.dumps(json_result, indent=2))
        return exit_code
    finally:
        close_ndjson()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, KeyboardInterrupt):
        raise SystemExit(EXIT_SIGINT)


