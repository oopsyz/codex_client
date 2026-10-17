# Protocol schema baseline: CLI 0.163.0

Verified 2026-10-16 against the current Windows x64 npm installation.
The [official App Server documentation](https://learn.chatgpt.com/docs/app-server)
defines generated schemas as specific to the CLI version used to generate them.

## Exact source

- Package: `@openai/codex` 0.163.0; `codex --version` reports `codex-cli 0.163.0`.
- Test command resolution: npm `codex.cmd`, then the package's Windows x64 binary,
  not the separately installed Desktop-bundled executable.
- Binary SHA256: `2692c8c7bb2cf8b0aeef638741fc90236f4b824c59578ce84994f9de58200993`.
- Standard generation: `codex app-server generate-json-schema --out <temporary-dir>`.
- Experimental generation: same command with `--experimental`.
- Hashing: parse JSON, serialize with sorted keys and separators `(',', ':')`,
  encode UTF-8, SHA256. No raw line-ending dependence.

The 17-file standard manifest in `tests/test_codex_ws_protocol.py` follows the
installed CLI. `tests/test_project_import.py` separately pins the two experimental
ProjectImport request/response schemas and validates wire fixture payloads. All
17 standard hashes match the checked-in `rust-v0.163.0` source snapshot; the
experimental schemas are pinned from CLI generation because the source tree does
not check in experimental schema files. Future installation drift must fail these
checks for explicit investigation; tests do not dynamically accept new hashes.

## Local reference comparison

Read-only reference: sibling `codex`, tag `rust-v0.163.0`, commit
`83086188ae0614bd882818ff1aa23f3194962012`. All 17 standard manifest hashes
match that checkout's `codex-rs/app-server-protocol/schema/json` snapshot. The
experimental ProjectImport schemas are not checked into that source snapshot.
Compared with the previous
0.154.0 baseline, 12 of the 17 standard schemas changed; the remaining five
matched. Changes relevant to this client include additional thread-list filters
and response metadata, thread lineage fields, file-ID image input support, and
four new standard notifications. `thread/rollback` was removed, and this client
does not call it. Existing request payloads remain valid; the notification
allowlist now recognizes the four added notifications.

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
