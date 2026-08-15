# TERRA Current Invocation Model Integration Plan

## Audience and rule of execution

This is an execution plan for a junior implementation model. Follow the gates in order. **Do not start by editing Refine.** A clean capability stop with no code changes is a valid and expected result.

## Mission

Make Refine use the exact effective LLM route of the Hermes invocation/session that triggered it, instead of the gateway-wide default or the most recently observed process-global model.

Observed failure:

- active Telegram session: MiMo;
- Hermes gateway default: `gpt-5.6-luna @ opencode-go`;
- `/refine status` and `/refine model`: Luna with `source: live`;
- Refine call attribution in existing history: Luna.

Luna is not hardcoded in Refine. The defect is route scope: Refine follows gateway/process state, not the invoking session.

## Required outcome

For a manual `/refine` command or model-originated `refine_run` tool call:

1. A deliberate trusted Refine command override or explicit plugin-config pin may retain its documented precedence.
2. Without an explicit override, Refine must use the invoking Hermes session's host-resolved route.
3. The route must preserve provider selection, model, endpoint, API mode, credential/profile selection, client behavior, cancellation, and authoritative actual-call metadata. Copying model/provider strings is not sufficient.
4. If Hermes cannot supply that invocation-bound route, Refine must return and persist a distinct sanitized capability outcome. It must not call gateway main and must not call the result `current`.

For automatic refinement, use a route bound to the triggering session/turn hook only if Hermes supplies one. Otherwise skip visibly with the same capability outcome. Do not substitute the gateway default.

## Current capability verdict

Read-only inspection of the locally installed Hermes source found no adequate public API. That checkout identifies itself as `0.16.0`. The expected target server is Hermes `0.17.x`; therefore the local finding is evidence about the likely integration gap, not proof of the target server contract.

Observed local contract:

- command handlers receive only `raw_args: str`;
- normal tool handlers receive tool args plus `task_id`/`user_task`, not a session route;
- `ctx.llm` is plugin-scoped, not invocation-bound;
- its default path uses process-global runtime-main metadata;
- gateway session overrides and complete provider bundles are private state;
- hooks expose fragments at some points, but no reusable invocation-bound facade.

Relevant host areas: `hermes_cli/plugins.py`, `gateway/run.py`, `gateway/slash_commands.py`, `agent/plugin_llm.py`, `agent/auxiliary_client.py`, `gateway/session_context.py`, and `model_tools.py`.

**Present conclusion:** a correct Refine-only fix is capability-blocked unless the exact imported target Hermes runtime exposes a newer public invocation API.

## Non-negotiable constraints

- Refine may create or patch; it may never propose deletion.
- `state.db` remains `mode=ro` and is never used to infer a model route.
- The base system prompt remains immutable.
- All trajectory text leaving the database continues through the central scrubber.
- Backup-before-edit, rollback-by-journal-id, and the daily budget remain unchanged.
- Add no dependency; use Python standard library only.
- Resolve Hermes paths only through `config.hermes_home()`.
- Tests and smokes use synthetic fixtures and temporary state only.
- Never read `.env`, `auth.json`, credential stores, private config, real trajectories, or real journal contents for this task.
- Never send real Telegram/session trajectory to an external model.
- Do not change gateway defaults, server model config, or `max_edits_per_day`.
- Do not rewrite historical Luna attribution or roll back any existing entry.

## Forbidden approximations

Never implement any of these as `current model`:

- `config.live_main_target()` or private `_read_main_provider()` / `_read_main_model()`;
- process-global `_REGISTERED_LLM` by itself;
- private gateway `_session_model_overrides`;
- `state.db`, journal rows, environment guesses, or session-ID guesses;
- a cache populated by API/LLM hooks;
- a new `PluginLlm` built from copied provider/model strings;
- changing the global default to match the test session;
- serializing calls to hide a global-state race.

These are stale, incomplete, private, concurrency-unsafe, or the wrong scope.

---

# Stage 0 — Verify the exact target Hermes capability

This stage is read-only. Do not edit Refine or Hermes.

## 0.1 Prove runtime identity

Expected target: the Hermes `0.17.x` runtime imported by the running gateway.

Record all of the following from the target server without opening user config or data:

- gateway Python interpreter path;
- imported package/distribution version;
- commit/build identifier when available;
- `__file__` paths for `PluginContext`, `PluginLlm`, gateway command dispatch, and tool registry;
- package metadata and inspected module paths when no commit identifier exists.

If the gateway imports a different installation/version than the one inspected, **stop and report the mismatch**. Branch A is valid only for the exact imported target runtime.

## 0.2 Trace the public contract

Provide file/line evidence for:

1. plugin command callback arguments;
2. model-originated plugin tool callback arguments/kwargs;
3. automatic session/turn hook arguments;
4. any public invocation/session context;
5. any host-owned facade or opaque route bound to that invocation;
6. the facade's completion methods and result metadata;
7. whether the API is public/supported rather than a private field.

## 0.3 Capability pass criteria

Stage 0 passes only if the exact host exposes a public object that:

- executes an auxiliary completion through the same immutable resolved route as the invoking session;
- keeps endpoint/profile/credentials host-owned and non-serializable;
- supports the completion/structured-output options Refine needs, including timeout/cancellation behavior;
- returns authoritative non-secret `reported_provider` and `reported_model` from the executed call, including after alias resolution, retry, or host fallback;
- can be supplied to commands, model-originated tools, and any automatic hook that is expected to run Refine.

Model/provider strings, a session ID, process globals, private dictionaries, and hook caches do not pass.

## 0.4 Mandatory branch

### Branch A — all criteria pass

Continue to Stage 2. Quote the exact API and source evidence in the implementation report.

### Branch B — any criterion fails

Stop immediately. Make **no Refine or Hermes source changes**. Produce a capability-block report containing:

- exact runtime identity;
- command/tool/hook signatures and invocation sites;
- `PluginLlm` default-routing path;
- missing route/call/result capability;
- why strings, globals, hooks, DB reads, and private gateway state are insufficient;
- the Stage 1 host prerequisite.

The report is the record for this discovery-time block. Do not add runtime journaling code under Branch B.

---

# Stage 1 — Hermes host prerequisite (separate, unauthorized scope)

Do not implement this in the Refine repository. Change Hermes only after separate explicit approval.

## 1.1 Minimum public contract

Conceptually, Hermes needs a non-secret invocation context:

```python
@dataclass(frozen=True)
class PluginInvocationContext:
    session_id: str | None
    session_key: str | None
    platform: str | None
    provider: str                 # display metadata only
    model: str                    # display metadata only
    llm: InvocationBoundPluginLlm # opaque host-owned route
```

The exact naming must follow Hermes conventions. The `llm` facade is the capability; provider/model strings are not.

The facade must expose the completion operations Refine already uses and return a result with authoritative actual-call provider/model metadata. It must preserve structured/JSON mode, output limits, timeout/cancellation, and normal error propagation. Raw keys, profile secrets, endpoint credentials, and reconstructable credential state must never be plugin-visible or serializable.

Backward-compatible callback shapes might be:

```python
command(raw_args: str, *, invocation: PluginInvocationContext | None = None)
tool(args: dict, *, invocation: PluginInvocationContext | None = None, **existing_kwargs)
```

These are examples, not instructions to invent an unsupported Refine shim.

## 1.2 Host binding requirements

Hermes must:

1. resolve the effective session route before plugin slash dispatch;
2. bind it task-locally, preferably with `ContextVar`, never a module global;
3. pass the same route to model-originated plugin tools;
4. pass it to automatic hooks or explicitly mark those hooks unsupported;
5. reset task-local state in `finally` after success, error, and cancellation;
6. preserve custom endpoint/API mode/profile/client behavior;
7. keep credentials host-owned;
8. return actual-call attribution after aliases/fallbacks;
9. remain correct under simultaneous sessions using different routes.

## 1.3 Required Hermes tests

Use synthetic providers and routes. Prove:

- gateway default Luna + session A MiMo: A's command facade calls MiMo;
- session B Luna remains Luna;
- overlapping A/B calls never cross routes;
- a tool receives its parent turn's exact route;
- automatic hooks receive the correct route or explicit unsupported context;
- command context exists before ordinary agent-loop setup;
- context clears after success, exception, and cancellation;
- alias/retry/fallback result metadata reports the provider/model actually used;
- older command/tool handlers remain compatible;
- unsupported contexts receive `invocation=None`, not a fabricated route;
- plugin-visible metadata/logs contain no credentials.

Only a released/installed host capability permits Branch A to continue.

---

# Stage 2 — Add failing Refine tests first

Proceed only after Stage 0 Branch A.

Use synthetic fakes and temporary files only. No network, real session, live DB, live journal, or live ledger.

## 2.1 Fixed route-selection table

Implement and test this exact precedence:

| Priority | Condition | Execution route | Failure behavior | Source label |
|---|---|---|---|---|
| 1 | trusted explicit command override | existing host-supported explicit override path | visible override/provider failure; no fallback | `command` |
| 2 | explicit plugin-config pin | existing host-supported configured path | visible config/provider failure; no fallback | `config` |
| 3 | invocation facade present | opaque host facade | visible call failure; no gateway fallback | `invocation` |
| 4 | none of the above | no call | persist/return `capability_unavailable` | `unavailable` |

If an explicit command override is denied by trust policy, return the existing explicit denial as a visible error; do not silently execute the invocation route. Only deliberate command/config overrides may use their existing string-based host API. They must never be labeled `invocation` or `current`.

## 2.2 Manual command and tool routing

For both ordinary `/refine` and model-originated `refine_run`:

- set gateway-main metadata to Luna;
- supply an invocation fake bound to MiMo;
- leave command/config overrides absent;
- assert only the MiMo facade receives the call;
- assert process-global/default Luna receives no call;
- assert no provider/model reconstruction from display fields;
- assert result attribution is the facade result's MiMo/provider, not context display strings.

Add a result-metadata test where context display says MiMo but the synthetic facade reports an alias/fallback model. Persist the authoritative facade result.

## 2.3 Real concurrency test

Start two synchronized, overlapping synthetic runs: A=MiMo and B=Luna. Assert each calls only its own facade. A sequential test does not count.

## 2.4 Display tests

For `/refine status` and `/refine model`:

- invocation context displays its model/provider with `source: invocation`;
- absent invocation never presents gateway main as current; label it `gateway_main`/`gateway_main_reference` or report current invocation unavailable;
- `command`, `config`, `invocation`, `gateway_main`, `host_default`, and `unavailable` are distinguishable;
- display makes no model call, consumes no budget, and mutates no journal/ledger.

## 2.5 Capability outcome tests

Define one sanitized non-applied operational outcome: `capability_unavailable`.

For manual command, tool call, and every automatic trigger, assert:

- it is returned visibly and persisted once through the existing journal outcome path;
- it is distinguishable from `no_op`, parse failure, and provider failure;
- it is persisted only after central scrubbing;
- no evidence is sent to a model and no LLM is called;
- no applied-edit budget slot is consumed;
- repeated persistence follows existing dedup/operational-record rules rather than flooding the journal.

This runtime record applies only after a supported Refine build is implemented. It does not authorize edits under Stage 0 Branch B.

## 2.6 Automatic-run tests

For every automatic trigger:

- exact hook-bound facade present: use it;
- facade absent: emit `capability_unavailable` before an LLM call;
- never use gateway main;
- overlap two different automatic session routes to prove isolation.

## 2.7 Overrides, attribution, and history

Assert:

- the fixed table is followed, including denied/failed override behavior;
- invocation facades are not blocked by plugin override trust flags;
- actual provider/model comes from the call result;
- route source is stored separately from actual-call attribution;
- old Luna audit rows remain Luna;
- `/refine audit` never relabels history from current context.

## 2.8 Compatibility

Test feature detection:

- supported host + facade: invocation route;
- supported Refine build + missing facade: capability outcome, no call;
- old host + deliberate trusted command/config route: only the explicit route, correctly labeled;
- old host + no deliberate route: capability outcome, no global fallback.

“No actionable improvement found” is not an acceptable capability result.

---

# Stage 3 — Minimal Refine implementation

Implement only what Stage 2 requires. Do not build a parallel Hermes router inside Refine.

## 3.1 Registration boundary (`__init__.py`)

- Accept the exact public invocation context contract.
- Thread its facade into the single command/tool run.
- Never store invocation context/facade in `_REGISTERED_LLM` or another global.
- Remove `_REGISTERED_LLM` from default invocation routing; retain it only for an explicitly safe non-invocation use, if any.
- Keep `status`, `model`, and `audit` free of calls and mutations.

## 3.2 Orchestration (`core.py`)

Thread the facade explicitly through `refine_run` and `_refine_once`. Apply the fixed table at one routing decision. When unavailable, stop before unnecessary evidence collection/model execution and emit the sanitized operational outcome.

Never make `config.live_main_target()` an execution default. Keep capability failure distinct from legitimate `no_op` in returned and persisted metadata.

## 3.3 LLM wrapper (`llm.py`)

Adapt only enough to call the facade while preserving:

- structured parsing/validation;
- shared output/content budget constants;
- timeout/error semantics;
- central scrubbing on output/persistence paths;
- `_record_call_meta` from authoritative actual-call result metadata;
- no route/client/secret serialization.

Do not rebuild a client from display fields.

## 3.4 Target metadata (`config.py` and display)

Separate execution capability from informational metadata. `gateway_main` and `host_default` are diagnostic references, never implicit invocation routes. If `live_main_target()` remains, relabel its user-facing meaning so it cannot claim to be current.

Avoid unrelated config refactoring.

## 3.5 Journal/ledger

Add only the minimum metadata needed to distinguish invocation, explicit override, capability unavailable, legitimate no-op, provider failure, and parse/runtime failure. Preserve actual result attribution, rollback, dedup, budgets, and old rows.

---

# Stage 4 — Local validation

From the Refine repository root on Windows:

```powershell
$env:PYTHONIOENCODING = 'utf-8'
python -m tests.run_tests
```

Read and record complete output; exit code alone is not evidence. Run the repository's existing compile/import check and inspect its output.

Required evidence:

- all old and new tests green, with skips reported;
- temporary fake SQLite plus temporary journal/backup/ledger only;
- no network or real Hermes state;
- no new dependency;
- no runtime artifact staged;
- focused diff only.

If aggregation, thresholds, or normalization change unexpectedly, stop and rescope; synthetic routing tests cannot validate those heuristic changes.

---

# Stage 5 — Installed-runtime synthetic smoke

Use the exact installed runtime validated in Stage 0, but isolate Refine completely before plugin registration:

1. Create a temporary fake SQLite DB with the test schema.
2. Create temporary Hermes home, journal, backup, and ledger directories.
3. Patch/inject every path resolver before importing/registering the plugin.
4. Install a guard that raises on any attempted file access outside the temporary root, except read-only imports from the installed source/runtime.
5. Use synthetic scrubbed evidence only.
6. Disable/replace all network/provider clients with fakes.
7. Register the actual plugin through the supported host API.
8. Dispatch command and tool callbacks with gateway reference Luna and invocation facade MiMo.
9. Assert MiMo wins, Luna is never called, and actual result attribution comes from the facade result.
10. Repeat with overlapping A/B routes.
11. Assert missing invocation produces the persisted temporary `capability_unavailable` outcome and no call.

If full isolation cannot be guaranteed, stop rather than run the smoke. If installed runtime identity differs from Stage 0, stop rather than add a speculative shim.

---

# Stage 6 — Narrow live read-only acceptance

This stage does **not** execute a Refine model call. Real Telegram trajectory must never be sent to an external model.

Use the existing persistent Telegram channel only to verify real slash dispatch and labels. Do not use `hermes -z`, a new ephemeral session, or a config workaround.

Preconditions:

- gateway default remains Luna;
- active Telegram session is MiMo through normal Hermes behavior;
- no default/model/budget config change;
- no rollback, cleanup, or edit action.

Sequence:

1. Capture Hermes's own current-session display showing MiMo.
2. Run `/refine model` and `/refine status` in that same channel.
3. Confirm Refine shows MiMo as `source: invocation`.
4. If Luna is also shown, confirm it is only `gateway_main_reference`, never current/execution route.
5. Confirm the read-only commands made no model call, applied no edit, consumed no budget, and changed no journal/ledger state.

Execution-routing evidence comes from Stage 5's isolated synthetic host dispatch, not from private live trajectory. State explicitly that a real-provider Refine completion against the user's live Telegram trajectory was **not run and not verified**, by privacy design.

If target Hermes lacks invocation capability, do not run a fallback. Return Branch B's capability report.

---

# Deliverables

1. Exact target-runtime identity and Stage 0 capability report.
2. If blocked: no source changes and the Stage 1 prerequisite report.
3. If supported: focused Refine diff plus synthetic tests.
4. Complete test/compile output summary, including skips.
5. Isolated installed-runtime smoke evidence.
6. Live read-only label/dispatch report, or an explicit reason it was not run.
7. Changed-file list and confirmation that no private/runtime artifacts were touched.

Do not commit, push, change server configuration, or edit Hermes unless separately requested.

# Final checklist

Before claiming success, answer explicitly:

- Was the exact imported Hermes `0.17.x` runtime inspected?
- Does it expose a public invocation-bound facade with authoritative result metadata?
- Is execution using that opaque facade rather than copied strings?
- Can overlapping sessions use different routes without crossover?
- Does missing capability stop before evidence/model execution and persist distinctly?
- Is the fixed override precedence followed without fallback?
- Are gateway-main and invocation labels unambiguous?
- Are display commands read-only?
- Does actual attribution come from the facade result?
- Are historical Luna rows unchanged?
- Are scrubbing, DB mode, budget, backup, rollback, and path invariants preserved?
- Did every test/smoke use synthetic data and temporary state?
- Was live acceptance limited to read-only commands?

Report every unknown as unverified. A green command or matching display string alone is not proof of correct routing.
