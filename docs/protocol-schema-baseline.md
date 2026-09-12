# Protocol schema baseline: CLI 0.154.0

Verified 2026-09-12 against the current Windows x64 npm installation.
The [official App Server documentation](https://learn.chatgpt.com/docs/app-server)
defines generated schemas as specific to the CLI version used to generate them.

## Exact source

- Package: `@openai/codex` 0.154.0; `codex --version` reports `codex-cli 0.154.0`.
- Test command resolution: npm `codex.cmd`, then the package's Windows x64 binary,
  not the separately installed Desktop-bundled executable.
- Binary SHA256: `be96b992178b1e467c225800da0d65f2c86d5eba1ef0b14632f65db381cbdfde`.
- Standard generation: `codex app-server generate-json-schema --out <temporary-dir>`.
- Experimental generation: same command with `--experimental`.
- Hashing: parse JSON, serialize with sorted keys and separators `(',', ':')`,
  encode UTF-8, SHA256. No raw line-ending dependence.

The 17-file standard manifest in `tests/test_codex_ws_protocol.py` follows the
installed CLI. `tests/test_project_import.py` separately pins the two experimental
ProjectImport request/response schemas and validates wire fixture payloads.
Future installation drift must fail these checks for explicit investigation;
tests do not dynamically accept newly generated hashes.

## Local reference comparison

Read-only reference: sibling `codex`, commit
`b1a547b1f73ce86205d9222ac19cff334b3b7a2e`. All 17 prior manifest hashes matched
that checkout's `codex-rs/app-server-protocol/schema/json` snapshot. Nine differ
from the installed CLI: ClientRequest, ServerNotification, ServerRequest,
ThreadListParams/Response, ThreadReadResponse, ThreadResumeParams/Response and
ThreadStartResponse. The other eight match. The reference was not changed and
is not claimed to be byte-identical to the installed release.

Observed changes include thread originator fields/filtering, reasoning
configuration history, thread environment definitions, and use of
LegacyAppPathString in permission-request cwd. Existing client payload tests
remain valid against the installed schemas. No client model, permission or
thread/turn policy change is inferred from this schema alignment.

The reference source independently supports project/import:

- `codex-rs/app-server-protocol/src/protocol/common.rs`: experimental method gate.
- `codex-rs/app-server-protocol/src/protocol/v2/project.rs`: name, roots,
  optional metadata/threads, required idempotencyKey; ProjectImportResponse.
- `codex-rs/app-server/src/request_processors/projects.rs`: one create/import
  store operation, then project/changed and thread/project/updated notifications.
- `codex-rs/app-server/tests/suite/v2/projects.rs`:
  `project_import_is_atomic_and_notifies_after_commit_in_order`, covering replay,
  duplicate thread and ephemeral-thread rejection. Inspected, not executed here.

The installed experimental schema agrees with those request/response fields.
The client deliberately requires nonempty roots and existing thread IDs for
this CLI operation; creating an empty project remains project/create's purpose.

## KEEP decision and proof boundary

KEEP: import fills a distinct need to associate existing persisted server threads
with a project. project/create plus thread/start only supports new threads.
No role-launch preparer or Desktop attachment workaround is required.

The local WebSocket fixture exercises the real client request path and error
handling, not a replacement in-memory client method. It checks exact parameters,
experimental initialization, pre-response notifications, output JSON, explicit
repeated invocation retaining the key, and no automatic retry after rejection,
overload or connection loss. A narrow CLI guard rejects create/import ambiguity;
RpcError is handled before its RuntimeError base class so server failures produce
the existing structured error response instead of an uncaught exception.

Fixture replay does not prove durable server transactions, live attachment or
Desktop readiness. No real App Server, role task, model call, profile installation
or remote operation was started by these tests. No private installation paths or
credentials are required in the committed fixtures.
