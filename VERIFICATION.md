# Verification ledger — iteration 0

Purpose: record, per observable behaviour, whether it has been **proven live** on a real Hermes
run, or only passes green against a fake DB / fake LLM. The bar for `PROVEN` is *quoted live
output*. A green test is `UNPROVEN` here by definition, no matter how many tests cover it.

This iteration makes **no source fix and no source commit**. It only builds the ledger.

> **Seed source (read first).**
> The `PROVEN` rows below are seeded from the round-14 live audit report, now copied into this
> workspace at `/home/ubuntu/round14-live-report.md` (originally `G:\Hermes\round14-live-report.md`).
> Every `PROVEN` row carries the quoted live output from that report. Two verdicts in that report
> were overstated and are corrected on the way in (see E-04b and E-05a): they are entered as
> `UNPROVEN`, not carried forward as proven.

## Ledger

| id | claim | status | evidence | iteration |
|---|---|---|---|---|
| O-01 | `no_op` emitted when no actionable improvement found | GREEN TESTS | 31 refs in suite/code; quoted live output not held | 0 |
| O-02 | `applied` emitted on a successful durable edit | GREEN TESTS | 30 refs; fake-DB FakeHost only | 0 |
| O-03 | `rejected` emitted when guardrails block | GREEN TESTS | 21 refs; no live quoted run | 0 |
| O-04 | `pending_approval` when host gates a write | GREEN TESTS | 16 refs; approval flow faked | 0 |
| O-05 | `rolled_back` emitted on a successful rollback | GREEN TESTS | 13 refs; deterministic test only | 0 |
| O-06 | `prepared` emitted when a skill write is staged | GREEN TESTS | 12 refs | 0 |
| O-07 | `failed` emitted on transaction-wide failure | GREEN TESTS | 14 refs | 0 |
| O-08 | `dry_run` emits a diff and applies nothing | GREEN TESTS | 5 refs; code path, no live run | 0 |
| O-09 | `conflict` when baseline drifted mid-plan | GREEN TESTS | 4 refs | 0 |
| O-10 | `journal_unreadable` fails closed | GREEN TESTS | 3 refs; `_load_entries_safe` mocked | 0 |
| O-11 | `evidence_unavailable` when session evidence fails | GREEN TESTS | 3 refs | 0 |
| O-12 | `evidence_invalidated` on rewound source evidence | GREEN TESTS | 6 refs; item-5 concurrency tests (fake DB + barrier) | 0 |
| O-13 | `skipped_session_source` when source in skip list | GREEN TESTS | 2 refs | 0 |
| O-14 | `session_unknown` when no session resolves | GREEN TESTS | 4 refs | 0 |
| O-15 | `llm_error` on provider/route failure | GREEN TESTS | 2 refs; retry telemetry unit test | 0 |
| O-16 | `llm_incomplete` on malformed/truncated proposal | GREEN TESTS | 2 refs | 0 |
| O-17 | `safety_blocked` on `local_safety` — **nothing tests it** | UNPROVEN (high) | 0 test refs; 4 emission paths in llm.py | 0 |
| O-18 | `target_issue` on unusable model target | GREEN TESTS | 4 refs | 0 |
| O-19 | `rollback` emitted by rollback command | GREEN TESTS | 2 refs | 0 |
| E-01 | Gateway restart proven by changed `MainPID` **and** `ActiveEnterTimestamp` | PROVEN | `MainPID=356838`, `ActiveEnterTimestamp=Fri 2026-08-14 13:21:39 UTC` — later than commit `a7b2251` `13:09:45Z` | 0 |
| E-02 | Skill create → rollback (journal `ac343ac173c1`) | PROVEN | applied → `hermes skills list` shows `git-commit-preflig…`; rollback → `✅ Rollback ac343ac173c1: Skill 'git-commit-preflight' deleted.`; after → `not in list`, dir absent | 0 |
| E-03 | Skill patch → rollback (baseline sha matched, content restored) | PROVEN | `refine_baseline.sha256=5832488…`; after rollback `sha256 5832488…` `match True`; after patch `sha256 b6410da4…` | 0 |
| E-04 | Memory rollback by `index`+`prefix_digest` (12→11) | PROVEN | recovery `index 11`, `prefix_digest b5a3db62…`; rollback → `Removed the exact appended memory entry`; `MEMORY.md` 12→11 | 0 |
| E-04b | Memory-rollback substring trap is impossible (by code path) | UNPROVEN | report says "substring trap verified by code path" and admits the live long-neighbour case was skipped for budget; code-path argument is reasoned, NOT tested | 0 |
| E-05 | Real concurrency: two parallel runs serialize on the mutation lock | PROVEN | P1 applied (journal `8945661f56ba` prepared+applied), P2 got `Daily edit limit reached (3)`; both ended at same 13:17:55; lock file absent after | 0 |
| E-05a | Multi-edit pending-approval live | UNPROVEN | report marks `proven (deterministic fake-host unit tests)` — fake host is GREEN TESTS by this ledger's bar, not live | 0 |
| E-06 | Design: mutation lock held across the whole LLM call | PROVEN (measurement) | P1 held lock 28 s (13:17:27→13:17:55); P2 blocked until release (report §Design finding) | 0 |
| S-01 | Green CI is real (suite runs N tests, not 0) | PROVEN | CI floor guard: `ran=666 floor=600` (b725c2d), probe went red `ran=0` (1beaeaa) | 1 |
| S-02 | `scripts` only; stdlib only; no new dependency | PROVEN (by inspection) | pyproject/stdlib imports verified in suite | 0 |
| S-03 | Scrubber uses positive allowlist for enum/count/identifier shapes (Task 5.5) | PROVEN (by inspection) | `_NON_SECRETS`, `_NUMERIC_METRIC_KEYS`, `_NON_SECRET_TOKEN_KEYS` are closed sets; denylist churn is bounded by decision (see S-03 note) | 0 |
| P-13 | Journal replay is material enough to justify caching? | PROVEN — NOT material | server journal: `_load_entries_state()` loads 410 entries, avg **5.75 ms** replay (min 5.13, max 6.17, 10 runs); lock acquire+release uncontended avg 17.1 ms; **do not cache** | 4 |
| O-20 | Clean install from the public repo registers and runs | PARTIAL | `hermes plugins install Bergschloss/Refine-Cycle-for-Hermes-Agent` → `✓ Installed ... Location: C:\Users\relig\AppData\Local\hermes\plugins\refine`; `plugins list --plain --no-bundled` → `enabled git 0.1.0 refine`; suite → `Ran 674 tests ... OK`; `status`/`audit` read the restored journal. Live `refine_run` NOT PROVEN: dry-run JSON `{"outcome":"llm_invocation_unavailable","failure":"llm_invocation_unavailable","message":"No invocation-bound host LLM is available; ... run install.sh from the plugin directory.","journal_id":"8af5d865199a"}` — core patch base `df4b6514` (v2026.8.16) is NEWER than host 0.20.1 (2026.8.13, `d5773bfc3`) | 5 |
| O-21 | Runtime data in `<HERMES_HOME>/refine/` survives plugin removal | PROVEN | journal line count 30 before remove and 30 after reinstall+enable (`_handle_refine_command('status')`: `journal=25944 bytes/30 lines`; `audit` → 8 refine-created entries read from the restored journal) | 5 |
| O-22 | Manifest format cannot express a host version requirement | PROVEN (by inspection + install) | `_read_manifest()` in `hermes_cli/plugins_cmd.py` is a bare `yaml.safe_load`; the only version key checked is `manifest_version` (line 737: `mv_int > _SUPPORTED_MANIFEST_VERSION` → refuse), which gates the *manifest schema*, not the host. `plugin.yaml` with no `requires` installed cleanly on 0.20.1; the runtime gate (`llm_invocation_unavailable`) is therefore the only available mechanism | 5 |
| O-23 | `install.sh` applies the route patch and restores/refuses cleanly on a real core | PROVEN | Against a pristine `df4b65147` worktree (symbol=0): (1) detect/no-op — live core already carries the route, `exit 0`, `git status`/commit unchanged, no backup dir; (2) apply+verify — `git apply (clean)`, symbol present (3), 9 touched files present, all compile; (3) idempotency — second run detects and `exit 0`, all 8 checksums unchanged; (4) apply-failure — corrupting a hunk anchor → honest refusal naming host HEAD `df4b65147d`, patch base `df4b65147d`, each failed attempt; **nothing modified** (git apply is atomic). **Found one defect**: the conflict-marker verify used `^(<<<<<<<|=======|>>>>>>>)`, which a decorative `====` banner in `agent/plugin_llm.py`/`plugins.py` docstrings falsely matched on a clean apply, triggering a needless restore; fixed to `^(<<<<<<< |>>>>>>> )` + added 2 regressions. NOTE: document said core `7211d2b9a`, real live core was `df4b65147` (patch base) — all release copies already carried the route, so apply/restore ran on a pristine worktree. Restore-on-verify-failure is covered by `test_verification_failure_restores_byte_for_byte` | 2026-08-25 |


> Note: round-14 `PROVEN` rows E-01..E-06 (and the seeded O-01..O-19 reusing that report's data) were proven **on the 08-14 codebase**. They are **superseded** on 2026-08-25: "proven on 08-14 code" and "proven on current code" are different claims. Do not delete them; the current-code Part-1 verdict is O-28 below (PROVEN, synthetic signal).

| O-24 | `model_substituted` surfaced on live; `requested_*` always filled, never silently passed off | PROVEN | dry-run llm_meta on current code (`038e501`, pin removed): `requested_provider=openrouter`, `requested_model=openrouter/free`, `target_source=invocation_bound`, `reported_provider=openrouter`, `reported_model=openrouter/free`, `output_tokens: 10`, **`model_substituted=false`** — requested always populated, reported captured, flag honestly computed | 2026-08-25 |
| O-25 | Full live cycle (dry_run → apply → `/refine audit` → rollback → confirm gone) on current code | UNPROVEN | four live dry-runs on current code (`494ae4e`, pin removed): host route `openrouter/free` → reviewer `llm_incomplete`/`malformed` (10 output tokens, too weak for structured JSON); session route `deepseek-v4-flash-vision-exp @ opencode-go` → reviewer `llm_route_error` (769 ms); `ox-alpha-free @ opencode-go` → reviewer `llm_route_error` (1019 ms) with the reviewer json_schema call failing because **opencode-go does not support `response_format.type=json_schema`** — a direct probe (2026-08-26) returned **HTTP 400 `invalid_request_error` "This response_format type is unavailable now"** on `json_schema` while `json_object` returns 200. The earlier errors.log reading of an **HTTP 503 "Endpoint is unavailable"** was a transient upstream symptom; the durable cause is the unsupported `json_schema` response_format, which is exactly the case the bound-route `json_mode` fallback (now added) recovers. **2026-08-26: the route fix is live-verified** — the bound reviewer/proposal call no longer returns `llm_route_error`; it now reaches the model (`reported=opencode-go/deepseek-v4-flash-vision-exp`). The apply/rollback chain is STILL UNPROVEN, now blocked by a distinct, diagnosable cause: the reviewer returns `no_final_text` because `REVIEWER_MAX_TOKENS=300` is too small for this reasoning model to finish a real ~16k-char trajectory (it emits >300 reasoning tokens and hits the cap; a direct probe of the same model/prompt on a SHORT trajectory returns valid `{"shouldRefine":false}` at 300–3000 tokens). **Provider limitation (json_schema unsupported) is fixed; remaining blocker is the reviewer token budget, not the route** | 2026-08-25 |
| O-26 | A `mode=ro` reader is **not** passive: it creates `-shm`/`-wal` and joins the WAL protocol | PROVEN | read-only `mode=ro` open of a live WAL `state.db` created both sidecar files; two Hermes builds sharing one `state.db` corrupted it 2026-08-25. `AGENTS.md` lists `mode=ro` as a safety invariant — it is one, *against writes*; it is not protection against two different SQLite builds touching the same database | 2026-08-25 |
| O-27 | The plugin always selects the **CURRENT** model (facade `llm.provider`/`llm.model`), never a stale config/live default | PROVEN | live dry-run with `-m deepseek-v4-flash-vision-exp --provider opencode-go`: `requested_provider=opencode-go`, `requested_model=deepseek-v4-flash-vision-exp`, `target_source=invocation_bound`, `model_substituted=false` — requested_* now name the current session model, not `openrouter/free`; configured via the facade route | 2026-08-25 |
| O-28 | Full live cycle on **current code** (dry_run → apply → `/refine audit` → rollback → confirm gone) — **synthetic signal** | PROVEN | commit `281a9f9`; session `synthetic-final-verification` (5 msgs, repeated tool error ×2 + explicit user correction, no paths/URLs): dry_run → `signal_path=gate_opened`, `output_mode=json_mode`, `model_substituted=false`, proposal shown (`c704a95e8e22`); apply → `outcome=applied`, `reversible=true`, created **memory** `test-run-strategy` (`bb15190e39a4`); `/refine audit` → ledger lists `memory:test-run-strategy` (`expects: "On a future test failure, the agent will run the affected…"`, verdict `too early`, model `deepseek-v4-flash-vision-exp`); rollback → `Removed the exact appended memory entry`; journal `prepared → applied → rollback_prepared → rolled_back`; **external memory-store check** (the profile's `MEMORY.md`, independent of the plugin journal) showed the entry after apply and its absence after rollback. **Caveats:** the signal is SYNTHETIC (not real trajectory); model `deepseek-v4-flash-vision-exp` @ `opencode-go`; applied `kind=memory`. The prompt-note path was NOT exercised to a successful apply on this model (see O-29) | 2026-08-26 |
| O-29 | On `deepseek-v4-flash-vision-exp` the **prompt-note path yields 0 applications**: the model consistently phrases the action non-conformant to `_PROMPT_NOTE_SAFE_ACTION` (`verify the expected endpoint before retrying`, `retry the request with exponential backoff`, `run the affected module first, then the full suite`), and the guardrail correctly rejects each with `"Prompt note action must match an approved behavioral policy"`. Conforming forms (`verify the endpoint before acting`, `confirm it before acting`, a valid SKILL.md, a path-free memory) pass `_validate_proposal` offline. Net on this model: refine applies only `kind=memory` (no path/URL) or `skill`, never `prompt` | PROVEN | reproduced live 2026-08-26: three consecutive apply runs all generated non-conformant `kind=prompt` proposals, all rejected by `_validate_proposal`; offline `_validate_proposal` confirmed the conforming forms pass | 2026-08-26 |
| O-30 | `apply` refuses when the memory store is full (~98%) | PROVEN (partially corrected) | apply of a 158-char memory proposal → `outcome=error`, `edits_applied=0`; the returned message **does** name the cause (`"Personal memory на 98.3%: 2,182/2,200 chars … Додавання перевищило б ліміт. Тому refine NOT applied."`), but the structured `refine_run` outcome is bare `error` — no distinct code, so a caller cannot detect the quota programmatically. Freed 382 chars by consolidating the agent's own memory → re-run applied successfully. **Correction to the stated finding:** the quota cause *is* named in the prose; what is absent is a distinct structured `outcome` for it | 2026-08-26 |
| O-31 | Part 2 census on the **restored server DB** → **EXPERIMENT NOT RUNNABLE** on this dataset | PROVEN (unavailable) | 237 message-sessions, 165 too short, 12 repeated-shape candidates; **11/12 are refine/plugin self-work** (circular — excluded); the 1 remaining is a **cron** session (`source=cron`, skipped by `skip_session_sources`) with a **spurious** signal (web_search results flagged by `_is_error_content`, see O-32). Runnable-census (has a `sessions` row, non-cron, valid timestamp): only **2** sessions, **both circular**. Result: **0 clean candidates → 0 proposals generated → no grading table**. The decision rule's "0–2 yes in column 4" branch presumes proposals were generated and graded; it **does not apply** here. **Verdict on the mechanism's value: OPEN, not negative** — the failure to run is a dataset problem (circular refine history + cron-skip + recovery-loss rows), not a mechanism defect. The same census on the pre-recovery snapshot / desktop DB is the path forward (see next) | 2026-08-26 |
| O-32 | `_is_error_content` false-positives on `<untrusted_tool_result source="web_search">` — web-search results counted as failures | PROVEN (measurement) | Recovered server DB: **1 distinct pattern (3 hits)** flagged solely by web_search results, **1 of which is a gate-tripping (≥2) pattern** → created a spurious "repeated-shape" candidate (the Jobcenter cron session in O-31). Pre-recovery snapshot: **0**. Same class as the historical 398-hit bogus pattern, but currently tiny (1 pattern). **Code NOT touched yet** — measured per plan before deciding | 2026-08-26 |


## Legend

- **PROVEN** — quoted live output (a real Hermes run) was produced and is recorded here.
- **GREEN TESTS** — the suite covers it against a fake DB / MockLlm (`MockLlm` used 183×,
  `PluginLlm` 44× in `tests/run_tests.py`); **not** seen on a live host.
- **UNPROVEN** — code exists but nothing proves it live. `UNPROVEN (high)` where nothing
  exercises it at all (see `safety_blocked`, O-17).

## What the suite relies on (so the reader knows what "green" means)

- Test classes: `RefineTests`, `TraceContractTests` (666 methods; discovered via
  `unittest.main`).
- The host is faked as `FakeHost`; the model as `MockLlm` (183 uses), `ProcessLlm(PluginLlm)`
  (2). **Nothing in the suite exercises the real provider route, the real gateway, or the real
  production `state.db`** (opened `mode=ro` only).
- This is why the ledger is lopsided by design, and why `safety_blocked` and the concurrency
  rows are the highest-priority non-live claims.

## Ordering for later iterations

Anything that mutates state or touches an invariant first: apply, rollback, budget, approval,
locking. Then the rest. A wrong `no_op` costs a run; a wrong mutation costs the user's agent.

## Not yet done (tracked elsewhere)

- 13-01 replication-latency measurement is **resolved** (see P-13): replay is 5.75 ms avg and
  **not material**, so no caching — a cache would add a new failure surface to the one file whose
  integrity everything else depends on, for zero measured benefit.
- Task 5.5 (scrubber decision) is **resolved by decision, not by code change** (see S-03).

## S-03 — scrubber allowlist decision (Task 5.5, resolved)

`sanitization.py` was touched in 15 of the last 15 commits that reached it, alternating tighten /
loosen. The plan's directive is explicit: do **not** treat "one more pattern" as progress.

The scrubber already inverted the one layer where the value shape is closed — an enum, count, or
identifier — into a positive allowlist:

- enum: `_NON_SECRETS = {true,false,null,none,enabled,disabled}` — a preserved value must be one
  of a closed set;
- count: `_NUMERIC_METRIC_KEYS` — a numeric value is preserved only for these exact telemetry
  field names;
- identifier: `_NON_SECRET_TOKEN_KEYS = {tokenizer, token_count}` — exact key names, not a broad
  substring exception.

Everything else (`_FIXED_PATTERNS` provider prefixes, `_ENV_SECRET`, `_AUTH_TOKEN`, quoted/unquoted
secret fields) is genuinely open-ended text where a positive allowlist of every safe value is
impossible. **Decision:** accept current coverage as a known limit rather than adding more
denylist patterns. A future pattern is added only if a concrete leaked secret is reproduced, never
by extending a word list on suspicion. This bounds the grind and removes the single-choke-point
denylist as a churn surface.
