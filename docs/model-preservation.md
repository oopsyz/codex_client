# Model omission on resume

Repair baseline: local main `1cb2109dc24a096bba68aaffd07b9907ca739017`.

The client previously resolved an omitted model before selecting start versus
resume, then sent it as a resume override. This could replace an existing
selection with the configured default. Default resolution now occurs only when
serializing thread creation. Caller omission survives resume and turn start;
explicit model and effort flags remain overrides. Creation includes one-shot,
detach, initial REPL, REPL `/new`, and TTL replacement.

## Protocol evidence (2026-09-16)

The official [Codex App Server documentation](https://learn.chatgpt.com/docs/app-server#start-or-resume-a-thread)
describes resume configuration as optional overrides and demonstrates resuming
without a model. Its lifecycle section describes turn fields as overrides too.
This establishes intended usage, not a universal persistence guarantee.

The installed npm CLI reports `codex-cli 0.154.0`. Its locally generated standard
schema (`codex app-server generate-json-schema --out <temporary-dir>`) makes
`ThreadResumeParams.model` optional (only `threadId` required). In
`TurnStartParams`, `model` and `effort` are optional overrides for this and
subsequent turns; only `threadId` and `input` are required. The existing pinned
schema manifest tests pass. Schema generation does not run a listening server
or invoke a model. Schemas establish accepted shapes, not inheritance logic.

Read-only local implementation reference: sibling Codex checkout, clean commit
`6b9826e3aa83b1a5947db50f4332cb9c65f1b340`:

- `codex-rs/app-server/src/request_processors/thread_processor.rs`,
  `merge_persisted_resume_metadata` and `has_model_resume_override`: absent
  model/provider/reasoning-config overrides permit persisted model/provider and
  reasoning metadata to populate the resumed configuration.
- `thread_resume_inner` restores that metadata before loading configuration;
  a persisted absent effort clears the configured effort when no model-related
  override exists. `load_and_apply_persisted_resume_metadata` depends on an
  available state database and matching thread metadata.
- `resume_running_thread` reads the existing configuration snapshot; overrides
  can be ignored for a still-observed running thread. Sending an explicit model
  on `turn/start` therefore also matters.
- `codex-rs/app-server/src/request_processors/turn_processor.rs` converts an
  explicit effort with `effort.map(Some)` and passes optional model/effort into
  thread settings overrides. Omission remains absent rather than resetting
  effort. The core thread-settings path passes those options into
  `StepSettingsUpdate`; `core/src/session/step_settings.rs` clones current
  settings and applies only the optional model/effort values.
  `protocol/src/config_types.rs::CollaborationMode::with_updates` uses existing
  model and reasoning effort when the corresponding option is absent.

This local source is not established as the exact source of the installed npm
binary. Missing persisted metadata, older servers, or server-side rerouting can
limit inheritance. No live reload or model turn was performed, and client wire
tests do not prove server persistence. The repair guarantees that this client
does not invent model/reasoning overrides; it does not reconstruct missing
server state or guess a prior selection.

## Convergent repair record

- MODE: convergent.
- SUBJECT: baseline above plus the model omission repair.
- EXIT: focused wire and protocol tests pass; bounded changes committed cleanly.
- CONVERGENCE RULE: repair demonstrated settings resolution/serialization defects
  and prove closure with focused tests.
- RUNAWAY SAFEGUARD: repeated same-root failure requires diagnosis; no repeated
  general review cycles.
- REVIEW NEED: Development checks CLI request serialization and protocol sources;
  no changed trust boundary or live operation requires an operational review.
- FINDING DISPOSITIONS: model default injected on resume, adopt now. Existing
  REPL connection close after a completed turn, defer as transport lifecycle
  work outside this settings repair.
- FINDING CLASS: demonstrated current-scope functional defect.
- REPAIR AUTHORITY: explicit user authority for local implementation, tests,
  documentation, and commit; no publication, installation, pin or live changes.

`tests/test_model_preservation.py` runs the real client against a synthetic
loopback WebSocket peer with a temporary configured model. It covers omitted
and explicit overrides across one-shot/detach/REPL, independent effort/model
flags, creation defaults, REPL `/new`, and TTL resume/replacement. Ordinary
resume asserts that default resolution is never called. Existing protocol
tests cover project/user config precedence and the built-in fallback.

The REPL `/new` test runs before its first completed turn. A separate resumed
REPL test checks turn serialization. This intentionally does not hide or repair
the pre-existing close-after-turn issue discovered during verification.

Validation: `python -m pytest -q tests/test_model_preservation.py
tests/test_codex_ws_protocol.py` passed **104 tests**, including validation of
captured start/resume/turn payloads against installed generated schemas and the
existing schema manifest checks. `git diff --check` passed and CLI help was
inspected. No full-system or live-server result is claimed.

VERDICT: STOP for the bounded functional repair; no unresolved current-scope
serialization finding remains. The transport issue and server-version/metadata
limitations above are retained explicitly.

## Installation implications

This commit changes canonical repository sources only. Installed skill copies
remain unchanged. A launcher that verifies the script SHA-256 must have its
pin updated to the exact installed bytes as part of a separately authorized
installation; copying the script alone can fail that check. Line-ending
conversion can affect that hash. No launcher configuration or SHA pin is changed
by this repair.

## Authorized extension: unsolicited session overrides

Extension subject: `9bc90cfaa406d1899e043af154d5fe5e8f80fa62` plus demonstrated
unsolicited instructions, personality, approval policy, and ephemeral fields.
The user explicitly authorized this additional local repair; the convergent
rule and repeated-root diagnosis safeguard above remain in effect.

Ordinary resume now omits developer instructions, personality, approval policy,
and ephemeral. Turn start omits unselected personality and policy as well as
model and effort. Explicit instructions, including an empty string, are sent
on thread start/resume; explicit personality and policy also reach turn start.
Fresh creation retains `Answer concisely.`, `pragmatic`, `never`, and
`ephemeral: false`. `/new` and TTL replacement follow those creation defaults.
Explicit `--ephemeral` remains supported for new threads and is rejected with
`--thread-id` before connection. CWD, roots, and named-profile omission and
explicit routing are retained, including the profile on turn start.

Official documentation was fetched again on 2026-09-16:

- [Start or resume](https://learn.chatgpt.com/docs/app-server#start-or-resume-a-thread)
  describes optional configuration overrides and demonstrates explicit personality.
- [Start a turn](https://learn.chatgpt.com/docs/app-server#start-a-turn) describes
  per-turn configuration overrides that persist to later turns.
- [Approvals](https://learn.chatgpt.com/docs/app-server#approvals) distinguishes
  server approval requests from client decisions, including decline and granting
  only a subset of requested permissions.

Freshly generated installed CLI 0.154.0 experimental schemas confirm optional
`approvalPolicy`, `personality`, and `developerInstructions` on resume, but no
`ephemeral` field. Turn start supports policy/personality but no top-level
developer instructions. Experimental schemas cover explicit roots/profiles.
Tests also reject outgoing keys not defined in the relevant schema, since
schema validation alone can permit unknown keys.

The same clean local source commit recorded above passes optional resume fields
through `build_thread_config_overrides` without manufacturing these values.
`load_and_apply_persisted_resume_metadata` restores persisted approval policy
when no override is present. In turn processing, `has_any_overrides` tests the
optional fields; core `StepSettings::apply` clones existing settings and changes
personality/policy only when supplied. This does not establish universal cold
reload inheritance of developer instructions/personality: configuration loading,
persisted metadata, and server versions remain relevant. The local source is
not proven identical to the installed binary. The client guarantee is omission
of unsolicited overrides, not a reconstruction of server state.

Approval safety remains client-owned and unchanged: noninteractive command/file
requests receive `decline`; permission requests receive an empty grant with turn
scope. Explicit policy alone never enables interactive decisions. Only
`--repl --interactive-approvals` enables prompts and selects `on-request` when
policy is omitted; an explicit policy wins. Inherited server policy is not an
approval or authorization to broaden access. Detached clients only answer while
connected and do not claim to resolve requests arriving after unsubscribe.

Extension validation: the same focused pytest command passed **111 tests**.
Wire coverage includes omission and explicit choices across one-shot/detach/REPL,
fresh defaults, explicit fresh choices, TTL and `/new`, omitted and explicit
CWD/roots/profile, interactive policy precedence, ephemeral rejection, and
noninteractive command/file/permission denials under inherited and explicit
server policy. No live task or server mutation was used. The prior REPL
close-after-turn limitation remains deferred. `git diff --check` passed.

Extension disposition: adopt the demonstrated unsolicited overrides; no
remaining current-scope finding. VERDICT: STOP after the bounded clean commit;
no installation, launcher pin change, publication, or live effect is authorized.
