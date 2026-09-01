# Spec — a full memory store must not burn the daily budget or lie about why

Handoff spec for a junior model. Three items, all bug fixes. No new capability is added:
nothing here teaches refine to free space, choose differently, or archive. That is a separate
feature decision and it is **out of scope**.

Baseline: `main` — read HEAD yourself. Suite is **875 OK (skipped=6)**; confirm before you
start and do not inherit the number.

---

## The measured problem

Hermes caps the memory store at ~2200 characters and, by design, **never auto-compacts**. On
overflow `tools/memory_tool.py` returns a consolidation failure — `"Memory at X/N chars.
Adding this entry (K chars) would exceed the limit. Consolidate now: use 'replace' ... then
retry this add"` — and expects the writer to make room itself. `curator.py` skips memory
entirely (`skip_memory=True`), so nothing ages entries out.

Refine does not make room, so it fails. That part is a design gap, not this task. What **is**
this task is that the failure is expensive and mislabelled:

1. The apply path journals `prepared`, then the host write fails, then it journals `error`.
2. `journal._CONSUMED_EDIT_OUTCOMES` contains `"prepared"`, and `count_today_applied()`
   counts any entry whose outcome is in that set. **The `error` never releases the slot.**
3. Measured on the live journal for one day: **12 `prepared`, 5 `applied`, 7 `error`** — about
   seven of ten daily slots consumed by writes that never landed.
4. The operator then sees the budget exhausted. The real cause — a full memory store — is
   reported as a generic `error`, so it cannot be counted or told apart from any other failure.

Point 4 is the same invariant this repo has already paid for twice: a failure that is not
distinguishable in the journal is invisible.

---

## Non-negotiable rules (AGENTS.md — these govern)

1. **Python 3 standard library only.** No new dependencies.
2. Suite from the repo root: `$env:PYTHONIOENCODING='utf-8'; python -m tests.run_tests`.
   Reading the output is the evidence.
3. **One commit per item.** Message explains *why*. Author is already configured — commit
   normally, never touch git config. **Push after every commit.**
4. No `__pycache__`, no scratch files. Delete probes before committing.
5. **Fail-first is mandatory** for all three: write the test, run it against the parent,
   **read** the failure, quote it in the commit message.
6. `git diff --check` clean and `python -m py_compile <file>` clean before each commit.
7. **`state.db` is opened `mode=ro`.** Always.
8. **DO NOT TOUCH `notify.py` or its tests.** A review of that file is in flight; a change
   there will collide. The memory figures the operator wants in the notification text are a
   separate commit that comes after that review lands. Nothing in this spec needs `notify.py`.
9. Do not add, remove, or reorder anything in `_CONSUMED_EDIT_OUTCOMES` /
   `_NO_ARTIFACT_OUTCOMES` without stating why — those sets are read by the budget gate and
   the audit, and widening the wrong one silently changes how many edits a day are allowed.

### Running against the live host

You may use the server to verify, and for items 1 and 3 you should. Read-only unless the item
says otherwise.

```
ssh oracle-imma
plugin checkout : ~/.hermes/plugins/refine        (git pull'ed; bare modules)
hermes source   : /home/ubuntu/releases/hermes-agent-v2026.8.31-clean
interpreter     : $HERMES/.venv/bin/python
journal         : ~/.hermes/refine-data/refine_journal.jsonl
run the suite   : cd ~/.hermes/plugins/refine && PYTHONIOENCODING=utf-8 \
                  /home/ubuntu/releases/hermes-agent-v2026.8.31-clean/.venv/bin/python -m tests.run_tests
```

- **Never edit `~/.hermes/config.yaml`** or any host configuration. Provider and model
  selection are settled; if something looks like it needs a provider change, stop and report.
- A long command must outlive the SSH session. `nohup`/`setsid` do **not** survive here —
  use a transient unit:
  `sudo -n systemd-run --unit=NAME --collect --uid=ubuntu --setenv=HOME=/home/ubuntu --working-directory=$HERMES --property=TimeoutStartSec=900 -- <cmd>`
- Never restart the gateway from inside a gateway turn. Use the same `systemd-run` pattern.
- The live journal contains the user's private trajectory. Report **aggregates only** — counts,
  outcomes, code names. Never paste entry content anywhere.

---

## Item 1 — a write that never landed must not consume a daily edit slot

**Files:** `journal.py` (+ tests)

**Why not the obvious fix.** Do **not** stop journaling `prepared` until the write succeeds.
`prepared` exists so that a process death *during* the apply leaves a durable record — there
is an existing test for exactly that ("A process death DURING `_append_entry`, not garbage
appended after it"). Removing it would trade a budget bug for a crash-recovery hole.

**The fix is in the counting, not the flow.** The journal is append-only and one edit can have
several lines sharing an `id` (**42 such multi-line transitions on the live journal**).
`count_today_applied()` currently counts *every* line whose outcome is in
`_CONSUMED_EDIT_OUTCOMES`, so a `prepared` line keeps its slot even after a later `error` line
for the same id says no artifact exists.

Resolve each `id` to its **latest** line first, then count that outcome:

- Group today's entries by `id`, keep the last occurrence in file order per id.
- Count the id only when its final outcome is in `_CONSUMED_EDIT_OUTCOMES`.
- `_NO_ARTIFACT_OUTCOMES` already names the terminal states where nothing exists
  (`error`, `rejected`, `rolled_back`, `cleanup_resolved`) — a slot is released only when the
  final outcome is one of those. **An `applied` id stays counted.**

**This change makes the budget more permissive, which is the dangerous direction.** So assert
the other side hard:

- An `applied` id counts, once, even with several lines.
- A `prepared` with **no** terminal line still counts — an apply that is genuinely in flight
  must hold its slot, or two concurrent passes could both think they have room.
- `pending_approval` still counts. A staged edit may yet land; releasing it would let the
  budget be exceeded while approvals queue.
- An id whose final line is `error` releases its slot.
- `daily_limit_reached()` must stay **fail-closed**: `count_today_applied()` already returns
  `max_edits_per_day()` when the journal is unreadable, and that must not change.

**Verify on the live journal (read-only)** before and after: report today's
`count_today_applied()` under both implementations and the difference, plus the count of
multi-line ids involved. Quote the numbers in the commit message.

**Fail-first:** a fixture with one id going `prepared` → `error` and one going
`prepared` → `applied`; the parent counts 2, the fix counts 1.

---

## Item 2 — a full memory store must be its own result, not a generic error

**Files:** `core.py` (+ tests)

`_apply_memory()` returns `_host_tool_result(memory_tool(action="add", ...))`. When the store
is full the host's payload is the consolidation failure quoted above. Refine turns that into
`outcome="error"` with the host text in `error`, and `result_code` falls back to the outcome —
so every such failure is indistinguishable from an unrelated apply error when counting.

**Fix.** Recognise the host's consolidation failure and give it its own code:

- Detect it from the host result, not from a loose substring of any error. Read
  `tools/memory_tool.py` on the server and key on what `_consolidation_failure()` actually
  returns — there is a structured marker as well as prose. **Prefer the structured field**; if
  you must fall back to text, require the specific phrasing (`would exceed the limit`) and say
  in the commit why the structured route was not available.
- Set `result_code="memory_full"` on the journal entry. Do **not** invent a new `outcome`
  value: `outcome` is read by the ledger and the audit, and adding a member there changes
  verdict logic. Keep `outcome="error"`, refine the code.
- Carry the numbers through. The host reports `X/N`; parse them and record `memory_used` and
  `memory_limit` in `llm_meta` so the operator can see how close it was without re-reading the
  prose. Parse defensively: if the shape ever changes, omit the fields rather than crash.
- Also detect the host's terminal "stop retrying" reply and code it distinctly
  (`memory_consolidation_exhausted`) — it is a different state from "one write did not fit".

**Both directions:**
- A full-store failure yields `result_code="memory_full"` with the two numbers.
- An unrelated memory apply error (host unavailable, malformed content) keeps its existing
  code and does **not** become `memory_full`.
- Scrubbing still applies: the host text goes through `scrub_text` on the way to the journal
  like every other error string.

**Fail-first:** the parent gives `result_code` equal to the outcome for a simulated
consolidation failure; the fix gives `memory_full`.

---

## Item 3 — refuse a memory edit that cannot fit, before attempting the write

**Files:** `core.py` (+ tests)

**Correction to an earlier framing, so you do not build the wrong thing.** This cannot be a
pre-flight check before the model call: `kind` is not known until the model has answered, and
refine may legitimately propose a `skill` or a `prompt` note, neither of which touches the
memory store. Blocking the pass on memory headroom would suppress proposals that were never
going to write there.

So the guard belongs at **proposal validation** — after the model, before apply. It saves the
host write attempt and (with item 1) the budget slot. It does not save the model call, and the
commit message should say so plainly rather than implying otherwise.

**Fix.** In the validation path that already rejects proposals (the function returning a
refusal reason for a proposal), add: when `kind == "memory"` and `action` would add content,
compute the projected size and refuse if it cannot fit.

- Read current size the same way `list_memory_snippets()` does — `MemoryStore()`,
  `load_from_disk()`, `memory_entries`. Compute the projected total the way the host does:
  the joined length using the host's own delimiter and limit constants. **Import them from
  `tools/memory_tool`; do not hardcode 2200 or the delimiter** — a copy that drifts from the
  host is a guardrail that reports the wrong answer.
- If the host constants cannot be imported (bare-module test runs), the guard must **degrade
  to allowing the proposal**, not to refusing it. A refusal on missing information would block
  legitimate edits on any host whose internals moved.
- On refusal, use the same `memory_full` code from item 2 so the two paths agree, and include
  the used/limit numbers.

**Both directions — this is where a mistake is expensive:**
- A memory create that cannot fit is refused, with `memory_full`, and **no host write is
  attempted** (assert the host tool was not called).
- A memory create that fits is unaffected.
- A **skill** or **prompt** proposal is unaffected even when the memory store is full — assert
  this explicitly; it is the whole reason the guard is here and not earlier.
- A `patch` that *replaces* content with something shorter must not be refused for lack of
  room. Compute the projected total for the post-edit state, not `current + new`.
- With the host constants unavailable, the proposal is allowed through.

**Fail-first:** the parent attempts the write and ends in `error`; the fix refuses at
validation with `memory_full` and never calls the host tool.

---

## Order and stop condition

Item 1 → Item 2 → Item 3. (2 before 3 because 3 reuses the code 2 introduces.)

Done when, each read rather than assumed:

- Three commits, pushed, author correct, fail-first output quoted for each.
- Suite green after every commit, count stated.
- Item 1's live-journal before/after numbers reported.
- `py_compile` clean, `git diff --check` clean, nothing stray committed.
- CI green **4/4 on the final SHA** — CI cancels in-progress runs, so confirm the last one:
  `gh run list --limit 1 --json databaseId` then `gh run view <id> --json jobs`.
- A closing note that says, in one line each: what now happens when the memory store is full,
  how many budget slots that costs (zero), and what still does **not** happen (refine does not
  free space — that is the open feature decision).

**Stop and report instead of improvising if:** `tools/memory_tool` exposes no stable way to
read the limit or detect the consolidation failure; item 1's change would release a slot for
an `applied` or `pending_approval` id; the memory-size read needs anything other than
read-only access; or a fix would require touching `notify.py`.

## Explicitly out of scope

- Teaching refine to `replace`/`remove`/archive to make room. Separate feature, not decided.
- Memory figures in the notification text. Blocked on the `notify.py` review.
- Changing `max_edits_per_day` (10 on the reference host is deliberate) or any host config.
- Anything in `docs/FINDING-*.md` — those are recorded decisions, not open work.
