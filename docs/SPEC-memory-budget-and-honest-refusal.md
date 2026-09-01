# Spec — stop the plugin littering memory, and stop a full store burning the budget

Handoff spec for a junior model. Two parts, six items, all bug fixes and guards. **No new
capability**: nothing here teaches refine to free space, archive, consolidate, or choose a
different store. That is a separate, deferred decision.

Baseline: `main` — read HEAD yourself. Suite is **875 OK (skipped=6)**; confirm it before you
start and do not inherit the number.

---

## Measured facts this spec is built on

All from the live reference host, not from reasoning.

**The store.** `memory_char_limit` is a Hermes config value — **4400** on the reference host
(the operator raised it from the 2200 default; the default is what most users will have).
Entries are joined with `tools.memory_tool.ENTRY_DELIMITER`, which is `'\n§\n'`. Current
state: **18 entries, 2149 chars used**.

**Entry sizes actually produced**, sorted:
`42, 45, 56, 60, 65, 65, 67, 84, 109, 119, 130, 134, 138, 150, 171, 206, 213, 244`
Median **124**. Only **3 of 18 exceed 200**, and those three are exactly the entries reviewed
as bloated or duplicated. A 200-char ceiling therefore trims the waste and leaves 15 of 18
untouched — that is where the number comes from, not from taste.

**Junk rate.** Of the last 6 applied memory edits, 3 were kept as useful, 2 were junk, 1 was a
near-duplicate of an entry already in the store. Two live entries say the same thing in 213
and 206 chars — **19% of a 2200 budget spent twice on one fact**.

**Fill rate.** 13 applied memory edits all time; 9 of them on one active day ≈ 1368 chars in a
day. Even with the junk removed that is ~700 chars/day of genuinely useful lessons. Guards slow
this; they do not stop it. Refine has no notion of a lesson becoming obsolete, so the store
grows monotonically and every user eventually stalls until they clean it by hand. **That is
understood and accepted here** — the archive/consolidation answer is deferred, not absent. Do
not try to solve it in this spec.

**Budget burn.** `journal._CONSUMED_EDIT_OUTCOMES` contains `"prepared"`, and
`count_today_applied()` counts every line whose outcome is in that set. A later `error` line
for the same id never releases the slot. One measured day: **12 `prepared`, 5 `applied`, 7
`error`** — about seven of ten daily slots consumed by writes that never landed, after which
the operator sees an exhausted budget while the real cause is reported as a generic `error`.

---

## Non-negotiable rules (AGENTS.md — these govern)

1. **Python 3 standard library only.** No new dependencies. No new files unless an item says so.
2. Suite from the repo root: `$env:PYTHONIOENCODING='utf-8'; python -m tests.run_tests`.
   Reading the output is the evidence; a command that ran is not.
3. **One commit per item.** The message explains *why*. Author is already configured — commit
   normally, never touch git config. **Push after every commit.**
4. No `__pycache__`, no scratch files. Delete probes before committing.
5. **Fail-first is mandatory** for every item: write the test, run it against the parent,
   **read** the failure, quote it in the commit message.
6. `git diff --check` clean and `python -m py_compile <file>` clean before each commit.
7. **`state.db` is opened `mode=ro`.** Always.
8. **DO NOT TOUCH `notify.py` or its tests.** A review of that file is in flight. Item G4 is
   therefore split: the data side lands now, the message text lands after that review.
9. **Never hardcode 2200, 4400, or the delimiter.** Read them from `tools.memory_tool` /
   the host config. A copy that drifts from the host is a guard reporting the wrong answer.
10. Do not change `_CONSUMED_EDIT_OUTCOMES` / `_NO_ARTIFACT_OUTCOMES` membership without
    stating why — the budget gate and the audit both read them.

### Running against the live host

Use it to verify; read-only unless an item says otherwise.

```
ssh oracle-imma
plugin checkout : ~/.hermes/plugins/refine        (git pull'ed; bare modules)
hermes source   : /home/ubuntu/releases/hermes-agent-v2026.8.31-clean
interpreter     : $HERMES/.venv/bin/python
journal         : ~/.hermes/refine-data/refine_journal.jsonl
suite on host   : cd ~/.hermes/plugins/refine && PYTHONIOENCODING=utf-8 $HERMES/.venv/bin/python -m tests.run_tests
```

- **Never edit `~/.hermes/config.yaml`**, the memory store, or any host configuration. Provider
  and model selection are settled.
- A long command must outlive the SSH session. `nohup`/`setsid` do **not** survive here — use
  `sudo -n systemd-run --unit=NAME --collect --uid=ubuntu --setenv=HOME=/home/ubuntu --working-directory=$HERMES --property=TimeoutStartSec=900 -- <cmd>`
- Never restart the gateway from inside a gateway turn; same `systemd-run` pattern.
- The journal and the memory store hold the user's private content. Report **aggregates only** —
  counts, lengths, codes. Never paste entry text into a commit message, a report, or a test.

---

# PART A — a failed write must not cost a budget slot or hide its cause

## A1 — release the daily slot when the write never landed

**Files:** `journal.py` (+ tests)

**Do not** stop journaling `prepared` until the write succeeds. It exists so a process death
*during* the apply leaves a durable record, and there is an existing test for exactly that.
Removing it trades a budget bug for a crash-recovery hole.

**Fix the counting instead.** The journal is append-only and one edit can span several lines
sharing an `id` (**42 such multi-line transitions on the live journal**). Resolve each `id` to
its **latest** line first, then count:

- Group today's entries by `id`, keep the last occurrence in file order.
- Count the id only when its final outcome is in `_CONSUMED_EDIT_OUTCOMES`.
- A slot is released only when the final outcome is in `_NO_ARTIFACT_OUTCOMES` (nothing exists).

**This makes the budget more permissive, which is the dangerous direction.** Assert the other
side hard:

- An `applied` id counts once, even across several lines.
- A `prepared` with **no** terminal line still counts — an apply in flight must hold its slot,
  or two concurrent passes could both believe they have room.
- `pending_approval` still counts. A staged edit may yet land.
- An id whose final line is `error` releases its slot.
- `daily_limit_reached()` stays **fail-closed**: `count_today_applied()` already returns
  `max_edits_per_day()` on an unreadable journal, and that must not change.

**Verify on the live journal (read-only):** report today's count under both implementations and
the delta, plus how many multi-line ids were involved. Quote the numbers.

**Fail-first:** a fixture with one id `prepared`→`error` and one `prepared`→`applied`; parent
counts 2, fix counts 1.

## A2 — a full store is its own result, not a generic error

**Files:** `core.py` (+ tests)

`_apply_memory()` returns `_host_tool_result(memory_tool(action="add", ...))`. On overflow the
host returns its consolidation failure (`"Memory at X/N chars. Adding this entry (K chars)
would exceed the limit. Consolidate now: use 'replace' ..."`). Refine turns that into
`outcome="error"`, so `result_code` falls back to the outcome and the cause cannot be counted
or told apart from any unrelated apply error.

- Detect it from the host result, keyed on what `_consolidation_failure()` in
  `tools/memory_tool.py` actually returns. **Prefer a structured field**; if you must fall back
  to prose, require the specific phrasing and say in the commit why the structured route was
  unavailable.
- Set `result_code="memory_full"`. Do **not** invent a new `outcome` value — `outcome` is read
  by the ledger and the audit, and adding a member changes verdict logic. Keep
  `outcome="error"`, refine the code.
- Parse `X` and `N` and record `memory_used` / `memory_limit` in `llm_meta`. Parse defensively:
  if the shape changes, omit the fields rather than raise.
- Code the host's terminal "stop retrying" reply distinctly
  (`memory_consolidation_exhausted`) — a different state from one write not fitting.

**Both directions:** a full-store failure yields `memory_full` with the numbers; an unrelated
memory apply error keeps its existing code; the host text still goes through `scrub_text`.

**Fail-first:** parent gives `result_code` equal to the outcome for a simulated consolidation
failure; fix gives `memory_full`.

---

# PART B — stop producing the junk in the first place

Measured junk rate is ~50% of applied memory edits. These four guards target the three
observed causes: self-correcting errors, bloat, and duplication.

## B1 — a self-correcting error is not a lesson

**Files:** `core.py` or `patterns.py` (whichever owns error classification) (+ tests)

`tool-search-requires-query` spent 106 chars recording that `tool_search` without `query`
returns `"query is required"`. The error text contains its own remedy; nothing is learned.

Gate these out **before the model call** — the error strings are already in the evidence, so
this costs no tokens at all.

**The risk is over-matching, and AGENTS.md is explicit about it:** a detector listing generic
words matches almost everything and yields zero signal. So:

- Narrow, anchored patterns only — a required/missing parameter named in the message, a
  "did you mean X" suggestion, a usage line. Not bare words like `error` or `invalid`.
- **Measure before shipping.** Run the gate over the live journal's error corpus and report how
  many distinct fingerprints it suppresses and which. If it suppresses anything that produced a
  lesson currently judged useful by `ledger.audit()`, the patterns are too wide — narrow them
  and re-measure. Put the numbers in the commit message.
- A suppressed error must still be **visible**: it is excluded from lesson candidacy, not
  deleted from evidence, and the pass must be able to say it saw it.

**Both directions:** the two known junk cases are suppressed; a real recurring failure whose
message happens to contain the word "required" is **not**.

## B2 — a memory entry has a length ceiling

**Files:** `llm.py` (prompt + validation) (+ tests)

Live distribution: median 124, and 3 of 18 entries exceed 200 — exactly the ones reviewed as
bloated. So:

- **Soft target in the proposer prompt: ~120 chars** for a memory entry, stated as one fact per
  entry. LLMs systematically overshoot soft limits, which is why the hard bar sits well above it.
- **Hard bar at 200.** Do not reject outright — refine already has a semantic retry
  (`allow_content_retry`, used for a `create` with no content). Reuse it: one retry asking for
  the same lesson under the limit. Only a second failure is a refusal
  (`result_code="memory_entry_too_long"`).

**The ceiling is absolute, not proportional to the limit.** A well-written lesson is ~130 chars
whether the store holds 2200 or 4400 — entry length is a writing-quality constraint, and
scaling it with the budget would license exactly the bloat this removes. What *does* scale with
the host limit is the usage reporting in B4.

Applies to `kind == "memory"` only. Skills have no such ceiling, and a prompt note already has
its own separate limit.

**Both directions:** a 260-char memory proposal triggers one shortening retry and lands if the
retry complies; a 130-char proposal is untouched; a skill proposal of 2000 chars is untouched;
two failures produce a refusal with the distinct code, not a silent drop.

## B3 — do not add what the store already says

**Files:** `core.py` (validation) (+ tests)

Two live entries state the same decision in 213 and 206 chars. The plugin confirmed its own
earlier lesson as if it were new.

- Compare the proposal against the **current** entries (`MemoryStore().load_from_disk()`,
  `memory_entries`) with token-set Jaccard similarity; refuse above a threshold with
  `result_code="memory_duplicate"`.
- Start at **0.55** and **calibrate against the live store**: the known duplicate pair must be
  caught, and no other existing pair may be flagged. Report the measured pairwise scores for
  all 18 entries so the threshold is chosen from data. If 0.55 flags a legitimate pair, raise
  it and say so.
- Tokenise on word boundaries, casefold, drop a small stop-word set. **The store is
  multilingual** — the live entries are Ukrainian and English. A whitespace tokeniser is fine
  for both; if you add a stop list, include Ukrainian function words, and do not assume
  English-only.

**Do NOT dedupe against entries the user has deleted.** A deletion is ambiguous — the reference
operator deleted a lesson for space, the failure recurred, and refine was right to raise it
again. Blocking that would make refine permanently blind to a real recurring failure, and the
suppression would be invisible. `journal.was_applied_recently()` already dampens rapid repeats;
that is the correct scope.

**Both directions:** the known duplicate is refused; a genuinely new lesson that merely shares
vocabulary with an existing one is accepted; an empty store never refuses.

## B4 — the operator must see the pressure at every write

**Files:** `core.py` (+ tests) now; the message text **later**, after the `notify.py` review.

The decision is that memory hygiene is the user's responsibility — which only works if the user
can see the state. Today usage appears only inside a failure.

- Compute used/limit on every applied memory edit and record `memory_used` / `memory_limit` in
  `llm_meta` (the same fields A2 adds on the failure path — one shape for both).
- Read both from the host: entries via `MemoryStore`, the delimiter via
  `ENTRY_DELIMITER`, the limit from the host's memory config. **Never hardcode.** The reference
  host is at 4400; the default most users have is 2200.
- If the host constants cannot be imported (bare-module runs), omit the fields rather than
  guess, and do not fail the edit.

**Deferred to a follow-up commit after the notify review lands:** the notification line becomes
`♾️ Refine Cycle — new lesson learned · memory 2149/4400`, plus `, getting tight` past a
percentage of the real limit. Do not touch `notify.py` for this now.

**Both directions:** an applied memory edit carries both fields with values matching a direct
read of the store; a skill or prompt edit does not gain memory fields; a host without the
constants still applies cleanly with the fields absent.

---

## Order and stop condition

A1 → A2 → B1 → B2 → B3 → B4. (A2 before B-items: they reuse its `llm_meta` fields and code
convention.)

Done when, each read rather than assumed:

- Six commits, pushed, author correct, fail-first output quoted for each.
- Suite green after every commit, count stated.
- A1's live-journal before/after numbers reported; B1's suppression measurement reported;
  B3's pairwise calibration reported.
- `py_compile` clean, `git diff --check` clean, nothing stray committed.
- CI green **4/4 on the final SHA** — CI cancels in-progress runs, so confirm the last one:
  `gh run list --limit 1 --json databaseId` then `gh run view <id> --json jobs`.
- A closing note, one line each: what happens now when the store is full, how many budget slots
  that costs, what the measured junk rate is after the guards, and what still does **not**
  happen (refine does not free space).

**Stop and report instead of improvising if:** `tools/memory_tool` exposes no stable way to read
the limit or detect the consolidation failure; A1 would release a slot for an `applied` or
`pending_approval` id; B1's patterns cannot be narrowed enough to leave every currently-useful
lesson intact; B3's threshold cannot separate the known duplicate from a legitimate pair; or any
item would need `notify.py`.

## Out of scope

- Archive, consolidation, `replace`/`remove` to make room. Deferred decision, not this batch.
- Falling back to a prompt note or skill when memory is full. Rejected: the operator reports
  that models do not reliably read those stores, so a write that lands there does not change
  behaviour.
- Changing `memory_char_limit` or any host config. It is 4400 on the reference host by the
  operator's deliberate choice; the plugin adapts to whatever it finds.
- A per-plugin quota or a `refine:` entry marker. Rejected: the marker costs the space it
  protects, and the journal already identifies refine's own entries.
- Anything in `docs/FINDING-*.md` — recorded decisions, not open work.

## Note on two junk entries, for the record

`refine-notify-v2-live-trace` (67 chars) and `refine-notify-v2-trace-two` (74) reached the store
during manual verification runs of the notification work. A manually triggered pass with an
explicit reason bypasses the recurrence gate by design, so this is not a code defect and no
guard is specified for it. The operational lesson is to use `dry_run` when driving refine for
verification.
