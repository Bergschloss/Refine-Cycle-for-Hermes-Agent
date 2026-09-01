# Finding — B12 backward-seek on count_today_applied is unsafe as specced

Status: **will not fix as proposed.** Measured on the live journal, not argued.

## The proposal

Audit 13-01 / spec B12: `count_today_applied()` reads the whole journal forward; replace with a
reverse read that stops at the first entry older than today's midnight.

## Why it is refused

`count_today_applied` counts entries where `outcome in _CONSUMED_EDIT_OUTCOMES` **and**
`ts.date() == today`. `ts` is the entry's **creation** time and is preserved across later
transitions — a `prepared` edit that finalizes or rolls back appends a **new line** carrying the
**same, old `ts`**. So a line late in the file can hold an old timestamp.

Measured on the live server journal (868 lines, 42 multi-line transitions):

```
ts decreases in file order 1 time
```

A reverse reader that stops at "first `ts < midnight`" therefore stops early on that
out-of-order line and **undercounts** today's consumed edits. Undercounting the daily budget
lets **more** than `max_edits_per_day` through — it weakens a guardrail. AGENTS.md rule:
`count_today_applied` returns `max_edits_per_day()` on failure precisely so the gate stays
**closed**; a fix that can undercount pushes the opposite way.

The spec's own acceptance fixture uses monotonic `ts`, so a buggy backward implementation would
pass it green. That is the "invisible on synthetic input, obvious on real data" trap this project
keeps hitting.

## The performance premise does not hold either

The spec assumed "800+ entries" is a problem. It is 868 lines; `_load_entries()` parses that in
single-digit milliseconds, and the budget check runs at most a few times per day. There is no
measured latency problem to trade a correctness risk against.

## What a safe fix would require

Not "stop at first old `ts`". It would need either:
- read backward but keep scanning until the file is exhausted OR a provable lower bound is
  reached (e.g. N lines where N ≥ max possible today-entries) — which on this size saves nothing;
- or a separate monotonic append-order timestamp that transitions do NOT preserve, so file order
  and cutoff agree — a schema change, out of scope and not worth it at this scale.

Left as-is. If the journal ever grows to where the forward read is measurably slow, revisit with
one of the above, and test against a journal that actually contains an out-of-order `ts`.
