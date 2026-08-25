# Verification ledger — iteration 0

Purpose: record, per observable behaviour, whether it has been **proven live** on a real Hermes
run, or only passes green against a fake DB / fake LLM. The bar for `PROVEN` is *quoted live
output*. A green test is `UNPROVEN` here by definition, no matter how many tests cover it.

This iteration makes **no source fix and no source commit**. It only builds the ledger.

> **Seed-availability note (read first).**
> The plan for this task assumed seed rows from `G:\Hermes\round14-live-report.md`
> (workspace A). That exact file is **not present** in this workspace (B,
> `/home/ubuntu/.hermes/plugins/refine`). The round-14 report that exists here
> (`/home/ubuntu/refine-autorun-round14-report.md`) is a *different* run — it reports
> `MainPID=285421`, a retry-telemetry fix, and a `Ran 545`/now-660 suite; it does not carry the
> `MainPID=356838` / `ac343ac173c1` / `5832488…` quoted chains the plan cites. Commit `105d0fc`
> present here describes a synthetic-DB reproduction, not a quoted live chain.
> Therefore every row that depends on `round14-live-report.md` is entered as
> `UNPROVEN (seed pending)` — not fabricated as proven. It must be re-verified against that
> document before it may be called live; the two overstated verdicts (multi-edit, memory-rollback
> substring trap) are deliberately **not** carried forward as proven.

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
| E-01 | Gateway restart proven by changed `MainPID` **and** `ActiveEnterTimestamp` | UNPROVEN (seed pending) | cited in round14-live-report.md, not present here | 0 |
| E-02 | Skill create → rollback (journal `ac343ac173c1`) | UNPROVEN (seed pending) | seed not present in workspace B | 0 |
| E-03 | Skill patch → rollback (baseline sha matched, content restored) | UNPROVEN (seed pending) | seed not present; `105d0fc` here is synthetic | 0 |
| E-04 | Memory rollback by `index`+`prefix_digest` (12→11) | UNPROVEN (seed pending) | seed not present; substring-trap argument is reasoned, NOT tested | 0 |
| E-05 | Real concurrency: two parallel runs serialize on the mutation lock | UNPROVEN (seed pending) | seed not present; live 28s hold reported but not re-verified | 0 |
| S-01 | Green CI is real (suite runs N tests, not 0) | PROVEN | CI floor guard: `ran=666 floor=600` (b725c2d), probe went red `ran=0` (1beaeaa) | 1 |
| S-02 | `scripts` only; stdlib only; no new dependency | PROVEN (by inspection) | pyproject/stdlib imports verified in suite | 0 |
| P-13 | 13-01 journal replay is material? | UNPROVEN (measured, TBD) | server journal 90 entries / 131,655 bytes — replay latency NOT yet timed | 0 |

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

- 13-01 replay-latency measurement (needs a real `_load_entries_state()` timing run) — do not
  cache until the numbers are material.
