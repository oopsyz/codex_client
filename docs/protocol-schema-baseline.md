# App Server schema baseline for Codex Desktop 0.171.0-alpha.13.2

Verified on 2026-11-23 with the Codex Desktop bundled Windows x64 executable.
Generated App Server schemas are specific to the CLI version that produced them.
[Official App Server documentation](https://learn.chatgpt.com/docs/app-server#message-schema)

## Exact source

- Desktop package `OpenAI.Codex_26.1120.5083.0_x64__2p2nqsd0c76g0`,
  executable `app/resources/codex.exe`. It reports `codex-cli 0.171.0-alpha.13.2`.
- Binary SHA256 is `6c5bb28fc2d290b649a060e4a88498511d0df9e51321781573b685ea20835a7e`.
- Schema hashes use SHA256 of parsed JSON serialized as UTF-8 with sorted keys
  and separators `(',', ':')`.

All schema-dependent tests read the absolute executable path from
`CODEX_APP_RUNTIME_CLI_PATH` and check the reported version before generating
schemas. Set that variable to the verified Desktop executable if the environment
does not supply it. The schema manifest tests compare generated hashes to the
checked-in pins.

To regenerate the bundles in PowerShell:

```powershell
& $env:CODEX_APP_RUNTIME_CLI_PATH app-server generate-json-schema --out ./standard-schema
& $env:CODEX_APP_RUNTIME_CLI_PATH app-server generate-json-schema --experimental --out ./experimental-schema
```

## Comparison with 0.163.0

The previous 17 standard pins match the source snapshot at `rust-v0.163.0`
(commit `83086188ae0614bd882818ff1aa23f3194962012`). Twelve changed in the
Desktop bundle. All literal client RPC calls remain in the generated method
schemas. The required fields for `thread/start`, `thread/resume`, `thread/read`,
`thread/list`, `turn/start`, and `turn/steer` are unchanged. Task search
continues to use `thread/list`, whose schema still accepts `cwd`, `searchTerm`,
and `sectionId`.

The standard notification schema adds `thread/section/changed` and
`thread/search/partial` compared with 0.163.0. The bounded client recognizes
every notification in the current standard schema. Its caller's profile and
validator still control admission.

The experimental `ProjectImportParams` pin matched the previous baseline.
`ProjectImportResponse` changed because the nested project dropped `position`.
The client still requires nonempty roots and existing thread IDs for
`project/import`.

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

Schema generation verifies the bundled executable's protocol contract. The
WebSocket tests use synthetic peers. Live task search, durable server transactions,
project attachment in Desktop, and model calls were outside this verification.
