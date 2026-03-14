from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Brainstorm-focused compatibility wrapper around "
            "`skills/claude-cli-client/scripts/claude_cli_client.py`."
        )
    )
    parser.add_argument("prompt", nargs="?", default="", help="Prompt text to send to Claude.")
    parser.add_argument("--thread-id", default="", help="Compatibility alias for Claude session ID.")
    parser.add_argument("--print-thread-id", action="store_true", help="Print `THREAD_ID=...` to stderr.")
    parser.add_argument("--prompt-file", default="", help="Read prompt from a file instead of the command line.")
    parser.add_argument("--cwd", default=".", help="Working directory to pass through to Claude.")
    parser.add_argument("--model", default="", help="Claude model alias or full name.")
    parser.add_argument("--system-prompt", default="", help="Override the Claude system prompt.")
    parser.add_argument("--append-system-prompt", default="", help="Append additional system prompt text.")
    parser.add_argument("--json-schema", default="", help="Optional JSON schema string.")
    parser.add_argument("--repl", action="store_true", help="Interactive loop that reuses a Claude session.")
    parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    parser.add_argument("--no-stream", action="store_true", help="Print only final text.")
    parser.add_argument("--timeout", type=float, default=0, help="Timeout in seconds for fresh sessions. 0 means no timeout.")
    parser.add_argument("--resume-timeout", type=float, default=300.0, help="Timeout in seconds for resumed sessions. 0 means no timeout.")
    parser.add_argument("--summary", action="store_true", help="Print token, latency, and cost summary to stderr.")
    parser.add_argument("--ndjson-file", default="", help="Path to append NDJSON trace records.")
    parser.add_argument("--out", default="", help="Save the final assistant text to a file.")
    parser.add_argument("--claude-bin", default="claude", help="Path to the Claude CLI binary.")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="Increase verbosity.")
    return parser.parse_args()


def build_command(args: argparse.Namespace) -> list[str]:
    skill_root = Path(__file__).resolve().parents[2]
    target = skill_root / "claude-cli-client" / "scripts" / "claude_cli_client.py"
    command = [sys.executable, str(target)]
    if args.thread_id:
        command.extend(["--session-id", args.thread_id])
    if args.print_thread_id:
        command.append("--print-session-id")
    if args.prompt_file:
        command.extend(["--prompt-file", args.prompt_file])
    if args.cwd:
        command.extend(["--cwd", args.cwd])
    if args.model:
        command.extend(["--model", args.model])
    if args.system_prompt:
        command.extend(["--system-prompt", args.system_prompt])
    if args.append_system_prompt:
        command.extend(["--append-system-prompt", args.append_system_prompt])
    if args.json_schema:
        command.extend(["--json-schema", args.json_schema])
    if args.repl:
        command.append("--repl")
    if args.json:
        command.append("--json")
    if args.no_stream:
        command.append("--no-stream")
    if args.timeout:
        command.extend(["--timeout", str(args.timeout)])
    if args.resume_timeout != 300.0:
        command.extend(["--resume-timeout", str(args.resume_timeout)])
    if args.summary:
        command.append("--summary")
    if args.ndjson_file:
        command.extend(["--ndjson-file", args.ndjson_file])
    if args.out:
        command.extend(["--out", args.out])
    if args.claude_bin != "claude":
        command.extend(["--claude-bin", args.claude_bin])
    if args.verbose:
        command.append("-" + ("v" * args.verbose))
    if args.prompt:
        command.append(args.prompt)
    return command


def main() -> int:
    args = parse_args()
    command = build_command(args)
    completed = subprocess.run(command)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
