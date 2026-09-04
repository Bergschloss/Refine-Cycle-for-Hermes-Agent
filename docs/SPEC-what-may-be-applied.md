# Spec — what may be applied, and how the run measures it

Baseline: HEAD `116120f`, plugin `0.14.1`. Suite at the time of writing: **1099 OK**, plus 11 skips of
`InstallScriptTests` on a Windows host whose `bash` resolves to a broken WSL relay. On Linux CI those
11 run. Confirm the numbers yourself; do not inherit them.

**This spec exists because the plugin is safe but not yet useful.** Across two graded runs it produced
**1 real lesson out of 7 proposals**. Nothing it did was unsafe: isolation held, `applied=0`
throughout, the size guard fired, the trivial gate closed correctly on sessions with nothing to learn.
The problem is the other direction — it writes notes that are not worth having, and once it wrote one
from a session where there was nothing to learn at all.

Items 1 and 2 are the release gate. Item 3 is the measurement that was broken by my own instructions.

---

## Rules (AGENTS.md governs)

1. **Python 3 standard library only.** No new dependencies.
2. Suite from the repo root: `$env:PYTHONIOENCODING='utf-8'; python -m tests.run_tests`.
   If bare `python` resolves to a Windows Store stub, use
   `C:\Users\relig\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe`.
3. **One commit per item.** Push after each. Author
   `263254659+Bergschloss@users.noreply.github.com`.
4. **Fail-first is mandatory.** Write the test, run it against the parent, **read** the failure, quote
   it. A test that never failed proves nothing.
5. `git diff --check` and `py_compile` clean before each commit.
6. New tests go in `tests/run_tests.py` — the only file CI executes.
7. **Do not change the live server.** No deploy, no gateway restart, no config edit, no journal edit.
   The daily budget of 10 there is deliberate for testing.
8. **Never weaken a guard to make a test pass.** If a guard is in the way, the guard is the finding.
9. CI cancels in-progress runs; confirm the run for your **final** SHA.

---

## Item 1 — A reviewer-approved pass may propose, but must not apply

**File:** `core.py`.

### What actually happens (verified by reading the code)

The gate closing is not the end of a pass. `_handle_no_signal` may call the reviewer, and when the
reviewer approves it returns `("reviewer_instructions", "reviewer_approved")` — `core.py:4111` — and
the primary proposal call proceeds as if the gate had opened. The preconditions are only
`reviewer_fallback_enabled()`, a message count over `reviewer_min_messages()`, and a cooldown.

**This is a deliberate feature, not a bug.** It exists to catch what the counting gate misses. Do not
delete it, and do not make the reviewer consult the gate — that would remove the only path that can
see a lesson the counter cannot.

### What went wrong

In the last run, `control-01` was a control session chosen precisely because no failure in it repeats
anywhere: 20 distinct one-off errors, `top_count=1`, gate correctly closed. The reviewer approved
anyway and the pass produced a memory entry about how effectiveness grading should work. Graded
**bloated**, 192 characters, and it is not a lesson about the agent's conduct at all — it is a note
about a test failure.

So the reviewer path can put an entry into durable memory with **no recurring evidence behind it**.
That is the plugin inventing a lesson where there was nothing to learn, which is the one outcome the
control sessions exist to detect.

### The fix

A proposal reached through `reviewer_approved` **may be proposed and journaled, but may not be
applied.** Concretely:

- Keep the reviewer call, the proposal, and the journal row exactly as they are.
- At the apply decision, refuse when the pass's `signal_path` is `reviewer_approved`, with an outcome
  that is distinguishable in the journal from every other refusal — not `rejected`, and not a silent
  `no_op`. Something like `reviewer_only`. AGENTS.md's rule applies: afterwards it must be possible to
  tell "the reviewer opened this and we declined to apply it" from "nothing was proposed".
- The refusal must **not** consume a daily edit slot. It applied nothing.
- `dry_run` behaviour does not change.

The value of the reviewer path is preserved: it still surfaces the proposal to the operator, in the
journal and the notification, where a human can act on it. What it loses is the ability to write
itself into memory unattended.

### Tests

- A gate-closed pass whose reviewer approves produces a proposal, journals it, and applies nothing;
  the outcome names the reviewer path; the daily budget is unchanged.
- A gate-**opened** pass with the same proposal still applies. This is the direction that proves you
  restricted the reviewer path and not the whole apply path.
- The refusal is distinguishable in the journal from `no_op`, from `rejected`, and from
  `daily_limit_reached`.

---

## Item 2 — An applied edit must be backed by recurrence worth the write

**Files:** `core.py`, `config.py`.

### What the data says

Every junk or duplicate proposal across both runs was backed by thin, single-session evidence:

| proposal | evidence behind it | verdict |
|---|---|---|
| `memory-tool-no-memorytool-export` | 1 hit, 1 session | junk |
| `dotnet` PATH note (earlier run) | 2 hits, 1 session | junk |
| `antigravity-timeout-retry` (earlier run) | 2 hits, 1 session | junk, and it inverted existing guidance |
| `github-permission-denied-retry` (earlier run) | 2 hits, 1 session | duplicate |
| `refine-terminal-verify-cwd` | 4 hits, 1 session, synthetic scratch path | duplicate |

And the two that were worth having were not thin:

| proposal | evidence behind it | verdict |
|---|---|---|
| `bash-timeout-rerun-check` (earlier run) | **10 hits** in one session, a loop that ran for months | real lesson |
| the gateway-lifecycle memory entry, applied in production 2026-09-04 07:49 | the same failure worked three times, converging | real lesson |

The gate opens at `min_pattern_count = 2`. That is the right bar for **gathering evidence and asking
the model**. It is plainly too low a bar for **writing into the agent's own future context.**

### The fix

Separate the two bars. `min_pattern_count` continues to govern the gate. Add a second, stricter bar
that governs **apply only**: an edit may be applied when the pattern behind it satisfies **either**

- `sessions_seen >= 2` — the failure recurred across separate sessions, or
- `count >= 5` — it recurred hard inside one session.

Otherwise the pass proposes and journals, and does not apply — same shape of refusal as Item 1, with
its own distinguishable outcome, and no slot consumed.

Both numbers belong in `config.py` next to `min_pattern_count`, following the same accessor pattern,
with the defaults above. **Derive nothing by feel:** if you find these two interact with an existing
limit, derive them from one constant, per AGENTS.md's rule about limits that must agree.

The proposal already carries `pattern_fingerprint`, and the aggregated patterns carry `count` and
`sessions_seen`, so the apply decision can look up what backs the edit. If a proposal carries no
fingerprint at all, it is by definition not backed by a measured pattern — treat it as not meeting the
bar, and say so in the journal.

### The consequence you must handle honestly

Cross-session patterns are **not collected** when a session id is given: `core.py:4871` returns an
empty list when `explicit_session` is set, and `__init__.py:1279` sets that flag whenever the
`refine_run` tool receives a `session_id`. So under this new bar, `/refine session <id>` can only ever
apply an edit via the `count >= 5` branch.

That is acceptable — naming a past session is an analysis request, not a mandate to edit memory. It is
**not** acceptable for it to look like "nothing to improve". Journal it as its own outcome and make the
tool result say plainly that the pass proposed without applying because a named session cannot supply
cross-session evidence.

### Tests

- `sessions_seen = 2, count = 2` applies. `sessions_seen = 1, count = 2` does not, and journals the
  reason. `sessions_seen = 1, count = 5` applies.
- A proposal with no `pattern_fingerprint` does not apply and says why.
- An explicit-session pass with a `count = 2` single-session pattern proposes, does not apply, and the
  journal distinguishes this from a quiet window.
- Neither refusal consumes a daily edit slot.

### Acceptance — measure it against real history, not only tests

The live journal holds **36 applied edits**. Replay them against the new bar, read-only, and report:

1. How many of the 36 would now be refused.
2. For a sample of the refused ones, quote the edit and say whether refusing it was right.
3. How many of the 36 were backed by `sessions_seen >= 2`.

Read the journal at `/home/ubuntu/.hermes/refine-data/refine_journal.jsonl` **through a copy**, or
locally if you have one. Do not write to the live host.

If the answer is "the new bar would have refused nearly everything", that is a finding to report, not
a reason to lower the bar quietly. Say the number and let it be decided.

---

## Item 3 — The usefulness run cannot measure cross-session, and that is my fault

**No code in `core.py` for this item.** It is the harness and the task document.

### What happened

I wrote `G:\Claude\hermes-crosssession-run-task.md` to measure the cross-session gate, and instructed
the runner to pass a `session_id` for each pass. Passing a session id is exactly what switches
cross-session collection off (`core.py:4871`, `__init__.py:1279`). The task's own instructions
disabled the thing the task existed to measure. The previous task had the same defect. The runner
reported `max_sessions_seen: 1` on all seven passes and correctly refused to treat the run as a
release gate.

A second defect, also mine: the sessions I supplied could not be reproduced by the runner. I claimed
`aaba620b8e67` spanned 6 sessions; the runner found it in 1. My selection was computed over a snapshot
with a different row filter than the harness applies, so the picks do not describe what the harness
sees. **Do not reuse those session ids.**

### What to build

A run that exercises the production path instead of imitating it:

- Drive the pass **without** a `session_id`, the way `on_session_end` does, so
  `collect_cross_session_patterns` actually runs. The window, not the session, is the unit of
  selection.
- Select the window by first measuring it with the **same filters the collectors use** — `role='tool'`,
  `active=1`, `_evidence_text_or_none` for admission, `patterns.fingerprint` over what it returns.
  A selection computed any other way is not describing the plugin's view. `~/refine-exp/pick_v2.py` on
  the server does it correctly; read it before writing your own.
- Keep every safety property of the existing harness: throwaway `HERMES_HOME`, `dry_run`, the abort if
  anything applies, the abort if the journal cannot be redirected, seeded journal, live journal and
  memory verified unchanged afterwards.
- Report `max_sessions_seen` per pass. If it is still 1, the run has failed again and must say so
  rather than grading proposals.

Controls still matter and get harder here: a control window must be one where no fingerprint repeats
**across** sessions either. Keep three.

Write the task document; do not run it yourself unless asked.

---

## What is already settled — do not re-litigate

- **The evidence fix works on real data.** Measured on a consistent snapshot of the live trajectory,
  `role='tool'` only: before, 506 fingerprints with 34 spanning ≥2 sessions, loudest was `refine_run`
  reporting its own daily limit; after, 452 with 23 spanning ≥2 sessions, loudest is a genuine
  `python: command not found`. Both `refine_run` patterns are gone.
- **The size guard fires.** Production, 2026-09-04: `Prompt note is too large for its per-note
  rendered context budget (158 chars; max 120)`.
- **The trivial gate works**, in both directions, in both runs.
- **`"error": null` is not a defect.** `exit 0 + error null` classifies as success; the pattern that
  looks like the old bug is `exit_code: 2`, a real failure.
- **Route failures and timeouts are a separate spec.** 267 route-unavailable and 113 timeouts out of
  453 failing passes are tracked in `SPEC-evidence-poisoning-and-route-failures.md`, items 2 and 3.
  They are not this spec's problem and must not be folded in.

## What I verified for this spec, and what I did not

**Verified by reading the code:** the reviewer path returns `reviewer_approved` at `core.py:4111` and
the primary proposal proceeds; `core.py:4871` empties cross-session patterns under `explicit_session`;
`__init__.py:1279` sets that flag from the tool's `session_id`.

**Verified by measurement:** the before/after admission table above; the guard messages from
production; the grading of all seven proposals across two runs.

**Not verified:** the thresholds in Item 2 are a judgement from seven graded proposals, not a fitted
number. That is exactly why the acceptance step replays 36 real applied edits — if the bar is wrong,
that measurement is what will say so.
