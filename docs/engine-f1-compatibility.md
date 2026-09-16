# F1: compose omission repair with maintained structured RPC errors

## Diagnosis and bounded repair

The independent 2026-09-16 settings integration froze Engine
`830cf47e7d7a181879f773ac45b7766e8349bbe6` with client
`92d31129baa09769656060bbf717d5c496bebd42`. Engine's production
`AppServerRuntime._client_module` rejects that client before connection because
`BoundedClientProfile.preserve_rpc_errors` is absent. This is reproducible, not
a flag-name mismatch to paper over.

The omission branch began on local `main` at `1cb2109`. At diagnosis, local
`main` was still there, while the locally recorded `origin/main` was `67e8e03`,
which already contains maintained capability commit
`e2b039b43162122ffdd969da0018c227c9fa785d`. No remote refresh or publication was
performed. The exact capability commit was cherry-picked with provenance into
the omission branch as `198dd14`; unrelated section-search work was not brought
into this bounded repair.

The adopted implementation adds real behavior, not just a capability flag:

- opt-in `BoundedRpcError` retains the correlated response, method, code,
  message and data; the Engine adapter receives this as `ProviderRpcError`;
- the connection survives a valid correlated error, with no automatic retry;
  a subsequent caller-decided request needs a fresh ID;
- default non-opt-in behavior still reports a generic error and closes;
- malformed errors, correlation violations, budgets, and server requests still
  fail closed. An observed error consumes its ID.

Engine's gate and transport code are unchanged. No Engine F2–F5 repair is part
of this change. Previous CLI model/reasoning/instruction/personality/policy
omission and noninteractive approval safety remain intact.

## Real configured composition proof

`tests/test_engine_f1_compatibility.py` is an explicitly opted-in composition
test, not a client runtime dependency on Engine. It requires the exact clean
Engine revision above. It calls the actual `general_launch_cli.configured`
function, which checks Engine revision and source cleanliness; then the real
hash-checking client loader, `_connection`, and `AppServerRuntime.send`.
No runtime/connection injection, gate patch, or replacement loader is used.
Only the remote peer is synthetic, listening on an ephemeral loopback port.
Fixture config, anchor and empty ledger are disposable temporary files; their
hash binding is not an update to any installed pin or configuration.

The proof covers:

1. The original `92d3112` client still fails the unchanged capability gate.
2. This candidate is actually loaded and the production connection selects
   `preserve_rpc_errors=True`.
3. A correlated server error with nested data survives into the exact Engine
   `ProviderRpcError`, with `BoundedRpcError` as its cause.
4. One error produces one read, not a retry. A caller-selected fresh-ID read
   then succeeds on the same connection.
5. Resume and turn parameters built by the loaded candidate reach the wire
   without unsolicited settings. Default model resolution is forbidden in
   that resume check.
6. Malformed errors, wrong IDs, duplicate IDs and server approval requests
   close the connection; the wire contains no approval answer or repeated read.

The accompanying protocol suite covers opt-in byte-budget enforcement and
default fail-closed behavior. The CLI omission suite exercises one-shot,
detach, REPL, fresh creation and client-side approval denials.

## Reproduction and observed result

From the client candidate checkout in PowerShell:

```powershell
$env:CODEX_F1_ENGINE_ROOT='C:/tmp/settings-integration-15893df893/engine'
$env:CODEX_F1_EVIDENCE_DIR='C:/tmp/codex-client-f1-proof-198dd14-r2'
python -B -m pytest -q -p no:cacheprovider tests/test_engine_f1_compatibility.py tests/test_model_preservation.py tests/test_codex_ws_protocol.py
```

Observed: **118 passed, 2 warnings** (existing Engine `jsonschema.RefResolver`
deprecation). `git diff --check` passed. The Engine and original frozen client
clones remained clean. Use a new evidence directory when rerunning: evidence
files are exclusively created and prior results are never overwritten.

Retained evidence directory: `C:/tmp/codex-client-f1-proof-198dd14-r2`.
`real-loader-error-read-omission.json` records outgoing frames and the exact
observed provider error; sibling files record the old-client rejection and
four fail-closed scenarios. Tested client script file SHA-256:
`bc3132db3644c9df152b72b61b442cdae344cf6e369b2a5d0cbfb4c621bbb09a`.
This is a tested-file digest, not an installation pin instruction; line endings
can change the installed-file digest.

The first run had 117 passing tests and one Windows fixture-cleanup failure:
an uncollected SQLite initialization connection held the disposable ledger
open. The test now collects that unreferenced connection after configuring the
operator. No Engine behavior was patched or failure assertion weakened. The
first evidence directory remains preserved separately.

## Review disposition and compatibility boundary

MODE: convergent. SUBJECT: original omission repair plus demonstrated F1.
EXIT: real frozen loader and structured-error behavior pass alongside omission
and fail-closed regressions, with clean local commits. CONVERGENCE RULE: repair
only demonstrated F1 client capability/composition failures. RUNAWAY SAFEGUARD:
repeated same-root failure requires diagnosis. REVIEW NEED: Development
composition evidence at the frozen loader and transport boundary. FINDING
CLASS: current-scope functional compatibility defect. REPAIR AUTHORITY: explicit
local F1 implementation, focused tests, documentation, and commit. DISPOSITION:
F1 adopted and closed for this exact composition boundary; F2–F5 remain
separately owned by Engine. VERDICT: STOP after local commit.

This establishes compatibility with the frozen Engine loader and structured
RPC-error contract. It is not a rerun or acceptance of the entire settings
lifecycle, live persistence, or deployed-server behavior. No existing task,
App Server, installed skill, checksum pin, registry or owner Engine source was
changed. No push, merge, or installation was performed.
