"""Generate test schemas with the pinned Codex Desktop runtime."""

import os
from pathlib import Path
import subprocess


SCHEMA_RUNTIME_VERSION = "codex-cli 0.171.0-alpha.13.2"


def generate_schema(out: Path, *, experimental: bool = False) -> None:
    runtime = Path(os.environ.get("CODEX_APP_RUNTIME_CLI_PATH", ""))
    if not runtime.is_absolute() or not runtime.is_file():
        raise RuntimeError(
            "Set CODEX_APP_RUNTIME_CLI_PATH to the absolute path of the "
            f"Codex Desktop executable ({SCHEMA_RUNTIME_VERSION})."
        )

    version = subprocess.check_output([str(runtime), "--version"], text=True).strip()
    if version != SCHEMA_RUNTIME_VERSION:
        raise RuntimeError(
            f"Expected {SCHEMA_RUNTIME_VERSION} from CODEX_APP_RUNTIME_CLI_PATH, got {version!r}."
        )

    command = [str(runtime), "app-server", "generate-json-schema", "--out", str(out)]
    if experimental:
        command.append("--experimental")
    subprocess.run(command, check=True)
