# Finding — A1's defect does not reproduce; the fix landed on 2026-08-15

Status: **no code change made.** Measured against the fail-first fixture the spec itself
specifies, then against the live journal.

## The claim

`docs/SPEC-memory-budget-and-honest-refusal.md`, item A1: `count_today_applied()` counts every
line whose `outcome` is in `_CONSUMED_EDIT_OUTCOMES`, including `"prepared"`, and a later
`"error"` line for the same id "never releases the slot." Cited evidence: "one measured day: 12
`prepared`, 5 `applied`, 7 `error` — about seven of ten daily slots consumed by writes that never
landed."

## Why it does not reproduce

`count_today_applied()` calls `_load_entries()`, which since commit `9790447` (`Characterize
journal replay before caching`, 2026-08-15) routes every read through `_replay_entries()`. That
function collapses the physical JSONL into **one logical record per id — its latest line** —
before any caller sees the list:

```python
if entry_id not in latest:
    order.append(entry_id)
else:
    _validate_journal_transition(latest[entry_id], entry)
latest[entry_id] = entry
```

`count_today_applied()` therefore counts each id once, by its **final** outcome. An id that
transitions `prepared` → `error` is counted as `error`, which is not in
`_CONSUMED_EDIT_OUTCOMES`, so it does not hold a slot. This predates the spec (dated
2026-09-01) by more than two weeks; the spec's own baseline note ("Baseline: `main`") did not
catch it because the fixture that would show the defect was never run against current `main`
before writing the item.

**Fail-first, run against current `main` (`22a4d8e`), using the spec's own fixture — one id
`prepared`→`error`, one id `prepared`→`applied`:**

```
SPEC EXPECTS parent=2, fix=1. ACTUAL result = 1
A1 defect DOES NOT REPRODUCE on current parent: multi-line id already resolves to latest
outcome before counting.
```

The three transition shapes A1 asks to be true today are already true:

- an `applied` id counts once, even across several lines — proven by `_replay_entries`
  returning one row per id;
- a `prepared` id with **no** terminal line still counts (in-flight write, unresolved) —
  verified directly: `count_today_applied()` == 1 after one bare `prepare()`;
- an id whose final line is `error` releases its slot — verified directly: `count_today_applied()`
  == 0 after `prepare()` → `finalize(..., "error")`.

**End-to-end through the real apply path** (`core.refine_run` with `FakeHost.fail_next` forcing
an apply failure, the same fixture `test_apply_failures_propagate_without_rollback_id` already
exercises): the entry finalizes to `"error"` and `count_today_applied()` reads `0` immediately
after, not `1`.

**Fail-closed on an unreadable journal is unaffected** — still returns `max_edits_per_day()`, so
this finding changes nothing about the safety direction AGENTS.md cares about.

## Verified on the live reference host (read-only)

`ssh oracle-imma`, journal at `~/.hermes/refine-data/refine_journal.jsonl`, 946 physical lines,
889 distinct ids, 51 multi-line ids.

- The real, installed `journal.count_today_applied()` — imported from
  `~/.hermes/plugins/refine`, run under the Hermes venv interpreter, against the live file —
  returns **0** for "today" at measurement time. `daily_limit_reached()` returns `False`.
- Comparing a **naive** re-implementation of the pre-fix counting rule (no id-collapse, sum
  every physical line whose outcome is in `_CONSUMED_EDIT_OUTCOMES` and whose own `ts` falls on a
  given day) against the **collapsed** rule the shipped code actually runs, over every day in the
  journal:

  ```
  day         naive  collapsed  diff
  2026-08-15     12          6      6  <-- DIFF
  2026-08-16      4          2      2  <-- DIFF
  2026-08-24     21         10     11  <-- DIFF
  2026-08-25      4          2      2  <-- DIFF
  2026-08-26      4          0      4  <-- DIFF
  2026-08-28     14          3     11  <-- DIFF
  2026-09-01     28          7     21  <-- DIFF
  ```

  Every day the two rules disagree, they disagree in the same direction: naive (pre-fix)
  overcounts, collapsed (shipped) undercounts relative to it — because collapsing removes exactly
  the double-counted `prepared` lines the spec is worried about. **This is direct evidence for
  the fix already having the intended effect on real data, not evidence against it.**
- Sampling the multi-line ids on the live journal: of the ones inspected, the transition shape is
  `['prepared', 'applied']` or `['prepared', 'error']` — never a `prepared` line left uncollapsed
  next to its resolution. No id demonstrates the "double counted" failure mode A1 describes.

No entry text was read or reported — aggregate counts and outcome/day tallies only, per
AGENTS.md.

## What this means for the spec's cited "12 prepared, 5 applied, 7 error" day

That count is consistent with the **naive**, pre-`9790447` counting rule, not with what
`count_today_applied()` has computed on this host since 2026-08-15. It was very likely gathered
by grep/manual tally over raw journal lines rather than by calling the function, or by reasoning
from the code's shape rather than running it. Either way, it does not describe current
production behavior.

## Disposition

A1 is **already fixed**, upstream of this spec, by `9790447`. No production change is needed or
made. This finding stands in place of a commit for A1; proceeding to A2, which is independent of
this item and still reproduces as specified.
