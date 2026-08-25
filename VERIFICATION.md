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
