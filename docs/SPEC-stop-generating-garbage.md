# Spec — stop generating garbage, not just stop applying it

Baseline: HEAD `4948ab1`, plugin `0.14.1`. Suite at the time of writing: **1103 OK**, plus 11 skips of
`InstallScriptTests` on a Windows host whose `bash` is a broken WSL relay; on Linux CI those 11 run.
Confirm the numbers yourself.

**Read this first, because it decides everything below.** The 8-pass run on `traj-A.db` produced four
proposals. All four were garbage. **The apply bar blocked all four** — `unbacked_pattern`,
`explicit_session_thin_evidence`, `reviewer_only`, `thin_evidence`. Nothing reached memory, the live
journal and memory were unchanged, `applied=0` everywhere.

So the safety layer works and is not in question. **Do not revert `4948ab1`.** What this spec fixes is
one layer earlier: the model is still shown garbage candidates, so it still spends a minute and 13k
tokens writing a note that a guard then throws away. Blocking output is not the same as not producing
it, and a proposal that only ever gets rejected is a proposal the operator has to read and dismiss
forever.

Target release: **0.14.2**.

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
7. **Never weaken a guard to make a test pass.** If a guard is in the way, the guard is the finding.
8. **Do not change the live server.** No deploy, no gateway restart, no config edit, no journal edit.
9. **Do not touch the 25-action prompt-note whitelist** (`llm.py:448`). It produces awkward wording —
   "verify the expected endpoint" for a local directory — but it is not what makes the proposals junk,
   and widening it introduces new validation risk in a release candidate. Out of scope. Say so if you
   disagree; do not act on it.
10. **Refine may create or patch, never propose a delete.** Unchanged.

---

## Item 1 — The model may only be offered patterns that could actually be applied

**File:** `core.py`, at the point where `error_patterns` is decided and rendered — the
`prioritize_signal_patterns(...)` call and the `evidence["error_patterns"] = ...` assignment that feeds
`_render_evidence_text`.

### What went wrong, measured

In `xsession-01` the gate opened on a genuine cross-session pattern with `max_sessions_seen = 3`. The
model then ignored it and proposed a note about fingerprint `0f457d47a0fa` — a terminal timeout seen
**once, in one session**:

```
"evidence": ["[1x across 1 session] terminal — timed out after 420.0s (fp:0f457d47a0fa)"]
```

The gate opened on a real pattern; the model wrote about the noise sitting next to it in the prompt.
This is the mechanism behind most of the junk, and no guard downstream can fix it, because by then the
model has already chosen its subject.

### The fix

Two rules, both applied before the evidence text is rendered:

1. **Never render a pattern that could not be applied.** A pattern reaches the prompt only when
   `sessions_seen >= config.apply_min_sessions()` **or** `count >= config.apply_min_occurrences()` —
   the same predicate the apply bar uses. Extract that predicate into **one** function and have both
   the renderer and the apply check call it. Two copies of this rule will drift, and AGENTS.md already
   names that failure mode.
2. **The proposal must cite an offered pattern.** `llm_meta` already carries `offered_fingerprints`,
   `fingerprint_offered` and `grounded`, so most of the machinery exists — **read it before writing
   anything new.** A proposal whose `pattern_fingerprint` is empty or is not among the offered
   fingerprints does not apply, and journals a reason that names which of the two it was.

Note the consequence, and keep it: when nothing clears the bar, the pass has nothing legitimate to
offer and must reach `no_op` **without calling the model at all**. That is a saving, not a regression —
but it must be journaled as its own outcome, distinguishable from "the model was asked and declined".
AGENTS.md's silent-`no_op` rule applies.

### Also exclude what is already covered

If a pattern is already addressed by an active prompt note or an existing memory entry, it is not an
unsolved problem and must not be offered. Use the note/memory text that is already loaded for the
proposer context (`_active_prompt_notes_safe`) — do not add a second read of those stores.

This is the check that would have stopped `xsession-01`: an active note `01369928b6d0` already says
*"When a process command times out, check timing assumptions before rerunning."* and the proposal was
*"When a terminal call times out, check timing assumptions before rerunning."*

### Tests

- A pattern with `sessions_seen=1, count=2` is not rendered; one with `sessions_seen=2, count=2` is;
  one with `sessions_seen=1, count=5` is.
- A pass where every pattern is below the bar reaches `no_op` **without an LLM call**, with its own
  outcome in the journal.
- A proposal citing a fingerprint that was never offered does not apply and says why.
- A pattern already covered by an active note is not rendered, while an uncovered one beside it is.
- The renderer and the apply check are proven to use the same predicate — change the config values in
  the test and both must move together.

---

## Item 2 — The plugin's own test and synthetic sessions are not evidence

**File:** `core.py`, in `_evidence_text_or_none` — the single admission point added in 0.14.1.

### What went wrong, measured

`signal-04` proposed a rule about verifying paths, built entirely on this:

```
[4x] terminal: cd: /home/ubuntu/.hermes/scratch/refine-synth-test-1788433382: No such file or directory
```

That directory is a scratch path created and destroyed by the plugin's own synthetic test. The agent
never had a real problem. Four of the historical applied edits with the highest counts (63x, 69x, 73x)
come from the same class — one of them literally reads *"Live trace artifact for the notifications-v2
audit. Safe to remove."*

### The fix — narrow, named, and no broader

Exclude a row from evidence when it references:

- a path under the plugin's own synthetic scratch prefix — `scratch/refine-synth-`, and
- this repository's own suite entry point — `tests.run_tests` (including `python -m tests.run_tests`).

**Do not filter on `AssertionError`, `pytest`, `unittest`, `Traceback`, or "test" as a word.** The
agent legitimately runs and debugs tests for the user's projects, and those failures are real
evidence. A filter that swallows them trades one kind of blindness for a worse one. If you believe a
third marker is needed, name it, show the rows it removes and the rows it wrongly removes, and ask.

### Tests

- A row naming `scratch/refine-synth-…` is not evidence; a row naming an ordinary
  `scratch/…` path still is.
- A row running `python -m tests.run_tests` is not evidence.
- A row containing `AssertionError` from an unrelated project **is** still evidence. This is the test
  that proves you did not over-filter.

---

## Item 3 — Prompt notes must deduplicate by pattern, not by wording

**Files:** `journal.py` (note storage), `core.py` (the duplicate check).

### The constraint you must not design around

I read the live store. A note is `{"content", "id", "scope"}` — **there is no fingerprint field**, and
there are **21 active notes**. So:

- New notes can carry a fingerprint. **Existing notes cannot be back-filled** — the fingerprint they
  came from was never recorded, and inferring it from the text is guessing.
- Therefore the existing text-similarity check **stays** as the fallback for legacy notes. You are
  adding a precise check, not replacing an imprecise one.

Anyone who deletes the similarity check because "fingerprints handle it now" has broken deduplication
for all 21 existing notes.

### The fix

- Persist `fingerprint` on every note the plugin writes from now on.
- Before proposing, treat a pattern whose fingerprint matches an active note as covered — this is the
  same exclusion as Item 1, and it should be the same code path, not a second one.
- At apply, refuse a note whose fingerprint already has an active note, with a journalled reason.
- **Re-learning stays legal.** If the user deleted the note, the fingerprint is no longer active and
  the lesson may be proposed again. Do not consult journal history to block it — deletion is a
  deliberate user decision, and AGENTS.md forbids turning it into a permanent ban.

### Tests

- Two proposals for the same fingerprint: the first applies, the second is refused as a duplicate.
- A legacy note with no fingerprint still blocks a near-identical restatement through the text check.
- A note deleted by the user does not block the same lesson from being proposed again.

---

## Item 4 — Fold the working 8-pass shape into the run task

**No plugin code.** The harness and the task document.

The 8-pass run finally measured what two earlier runs could not: the `xsession-01` pass, driven
**without** a `session_id`, reached `max_sessions_seen = 3`. That shape works and must not be lost —
the per-session passes cannot measure cross-session evidence at all, because a named session disables
cross-session collection (`core.py:4871`, `__init__.py:1279`).

Update the run task so the standard run is: 4 signal sessions, 3 controls, **and** at least one
cross-window pass with no `session_id`. Keep every existing safety property — throwaway
`HERMES_HOME`, `dry_run`, seeded journal, abort if anything applies, live journal and memory verified
unchanged afterwards, throwaway home removed.

Then re-run it against your finished work and report with the same grading table.

---

## Release criteria for 0.14.2

Ship when all of these hold on a fresh 8-pass run:

1. Integrity clean: live journal and memory unchanged, `applied=0`, throwaway home removed.
2. No control session produces a proposal.
3. No proposal is grounded in a pattern below the apply bar — the model cannot be offered one.
4. No proposal duplicates an active prompt note or memory entry.
5. A pass with nothing above the bar reaches `no_op` **without an LLM call**, and the journal says so
   distinguishably.
6. The cross-window pass reports `max_sessions_seen >= 2` and either proposes something grounded in
   that pattern, or honestly declines.

A run that produces **zero** proposals satisfies these and is a pass, not a failure. Given the last
four proposals were all junk, zero is an improvement.

---

## Appendix — legacy entries already in production memory

Written before the 0.14.1 evidence fix, so no new ones will appear. Removing them is the operator's
call, via the plugin's own `/refine rollback <id>`, which is the one legitimate delete path.

Test artifacts:

- `af6f5b231380` — "Live trace artifact for the notifications-v2 audit. Safe to remove."
- `acab7fe0db1e` — "Second live trace artifact… Safe to remove."
- `5e8158c67e51` — skill `synthetic-trace-skill`, content `# synthetic`

Notes about refine itself:

- `6e7cf540cdad`, `27c91e4545cb`, `e96ca77d02cf`, `b6d99f583be0`

Stale number:

- `323f638d20ff` — "memory limit 2200; shorten if a batch exceeds it". The live limit is **4400**
  (`config.yaml: memory_char_limit: 4400`, and `core._memory_usage()` returns `(646, 4400)`). The
  entry understates the budget by half, so the agent trims when it does not need to. This is the most
  harmful of the eight, because the agent will act on the number.

**Do not build a general "stale fact" detector for this release.** One stale entry is not a feature
request. If it recurs, it earns its own spec.

---

## What I verified, and what I did not

**Verified by reading the live host:** prompt notes are `{content, id, scope}` with no fingerprint
field, 21 active; `memory_char_limit: 4400` in `config.yaml`; `core._memory_usage()` → `(646, 4400)`.

**Verified by reading the code:** `core.py:4871` empties cross-session patterns under
`explicit_session`; `__init__.py:1279` sets that flag from the tool's `session_id`; `llm_meta` already
carries `offered_fingerprints`, `fingerprint_offered` and `grounded`.

**Taken from the audit report, not re-measured by me:** the four proposals and the four rejection
reasons from the 8-pass run, and `max_sessions_seen = 3` on `xsession-01`. If any of that does not
reproduce, say so before building on it.

**Not verified:** that Item 1 removes most of the garbage. It is a hypothesis with one strong data
point (`xsession-01` chose a `1x` pattern while a `3`-session pattern was in front of it). The re-run
in Item 4 is what tests it. If proposals stay junk after Item 1, the cause is the prompt or the model,
not the candidate list — report that rather than adding more filters.
