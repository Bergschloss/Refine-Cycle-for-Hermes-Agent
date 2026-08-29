# HYPOTHESES.md — where the known defect shape is still hiding

Written against `main` at `2449486` (v0.11.0, 752 tests, CI green), after two audits that
found nothing CRITICAL. The question asked was not "review the code" but: **given sixteen
real defects that almost all share one shape — a component confidently reporting on
something it could not see, or a check that only ever tested one direction — where is the
next one, and what single probe settles it?**

Ranked by consequence if true.

**Status after this pass:** H1 and H2 were confirmed and fixed in the same pass — `d4bfe70`
(classification reads the raw row) and `0e12ca0` (the session budget is spent on failing
sessions), suite 756 OK, both measured against a live database before and after. Their
sections below are kept as written, because the reasoning is the part worth reusing. H3 is
open; H3 confirmed by probe on this repo and FIXED (4701f36). H4 CLEARED on the desktop host but CONFIRMED on the server (garbage timestamps in the live DB); H5 re-measured on the server: 6 of 316 sessions carry >=1 correction, 3 with no tool error — single-event exposure real but small.

Everything below was measured on a **live desktop install** (`%LOCALAPPDATA%\hermes\state.db`,
2694 active tool rows, 170 sessions, 58 journal entries, 5 applied edits) using read-only
queries. Only aggregate counts left that machine; no trajectory content was read into a
model, quoted here, or committed. Where a claim is synthetic, it says so.

---

## H1 — The credential choke point can invalidate JSON, and error classification runs *after* it

**What could be wrong.** `scrub_text` replaces an unquoted value with the bare token
`[REDACTED]`, which is not a JSON scalar; both evidence paths classify the **scrubbed**
string, so `_structured_error_status` loses its verdict on those rows and the decision falls
back to the head/tail marker heuristic — the exact mechanism of defect 2.

**Why this codebase produces it.** Invariant 4 forces every row out of the database through
one scrubber, and both call sites obey it (`core.py:1241`, `core.py:1364`). Nothing
downstream is aware that the scrubber is not JSON-transparent. `_NUMERIC_METRIC_KEYS`
protects `total_tokens`, `prompt_tokens` and six siblings from redaction — evidence that this
collision was noticed once, for telemetry keys, and closed only for the names then in front
of someone. Plain `tokens`, `tokens_used`, `session_id` and `api_key_id` are not in that set.
This is a check that only ever tested one direction: the tests prove secrets get redacted;
none proves a valid JSON payload is still valid afterwards.

**Measured, live corpus (81 days, 2694 tool rows):**

```
scrub_changed=330  json_parsed_raw=305  json_broken_by_scrub=27
structured_verdict_lost=29  classification_error_to_ok=0  classification_ok_to_error=0
```

27 of 305 JSON tool rows (8.9%) stop parsing after scrubbing; 29 rows lose their structured
verdict outright. The mechanism is live. The corpus happens to contain no row where the
marker fallback then *disagrees* with the structured verdict — so the harm is latent here,
not absent.

**Synthetic proof of the harm direction** (`python -c`, no host needed):

```
raw    {"success": false, "session_id": 918273645, "detail": "nope"}
       structured=True  is_error=True
scrub  {"success": false, "session_id": [REDACTED], "detail": "nope"}
       structured=None  is_error=False      <-- a genuine failure, classified as a success
```

**Consequence if true.** False negatives in the plugin's only input. A repeated failure whose
payload carries a numeric credential-shaped field never becomes a pattern, is never counted,
never reaches the gate — and nothing logs that it was dropped.

**The one probe.** Run against the server's `state.db`, read-only:

```python
# for every active role='tool' row whose content the scrubber changes:
#   parses_raw    = json.loads(raw) succeeds
#   parses_scrub  = json.loads(scrub_text(raw)) succeeds
#   verdict_lost  = _structured_error_status(raw) is not None
#                   and _structured_error_status(scrub_text(raw)) is None
#   flip          = _is_error_content(raw, tool) != _is_error_content(scrub_text(raw), tool)
# print counts only.
```

**Confirms it:** any nonzero `json_broken_by_scrub` or `verdict_lost` — the mechanism exists
and one payload shape away from a flip. A nonzero `flip` count is a live defect with a row
count attached.
**Clears it:** zero broken rows *and* an argument that no unquoted credential-shaped field
can appear in host tool output. The first alone is not enough; the corpus is one machine.

**Minimal fix shape.** Compute the structured status from the raw string inside the same
boundary function, before scrubbing, and scrub what is stored and rendered. A bool leaves the
boundary, not text, so invariant 4 is untouched. The alternative — emitting a quoted
`"[REDACTED]"` for JSON-scalar values — keeps parsing alive but interacts with the
forged-marker rules and changes the marker shape everywhere. Prefer the first. Test both
directions: a genuine error with a numeric secret field stays an error; a success carrying
the word "error" in a payload string stays a success.

---

## H2 — The cross-session window spends its session budget on recency, not on failure

**What could be wrong.** `collect_cross_session_patterns` admits a session slot for *any*
tool row (`core.py:1364-1372`), before the `_is_error_content` filter that decides whether
the row matters. The newest 25 sessions consume all 25 slots whether or not they contain a
single failure, so `sessions_seen` — the stronger half of the signal gate — is measured over
a fraction of the sessions that actually contain failures.

**Why this codebase produces it.** This is defect 1 exactly, one layer up: a bound applied
before the filter that decides relevance. Defect 1 was fixed *inside* the pattern layer, and
the comment at `core.py:1384` now asserts the completeness the fix bought — "Signal gating
must see every observed pattern … truncating here cannot hide a qualifying lower-ranked
failure from `has_signal()`". True of `FORMAT_PATTERNS_LIMIT`. Not true of the two bounds
upstream of it in the same function. The row cap at least logs a warning when it binds
(`core.py:1388`); **the session cap logs nothing.**

**Measured, live corpus (7-day horizon, defaults `max_rows=4000`, `max_sessions=25`):**

```
sessions_with_errors_in_7d=66   error_sessions_reachable_by_current_query=12
session_slots_spent_total=25    (13 of the 25 slots went to sessions with no errors at all)
```

The cross-session spread of every pattern is computed over 18% of the sessions that contain
failures. `max_rows` was not binding here (852 rows in the window); the session cap was.

**Consequence if true.** `has_signal`'s cross-session criterion — the one the docstring calls
"stronger evidence than the same count inside one session" — under-measures spread roughly
fivefold, so the gate stays closed on precisely the chronic, spread-out failures the plugin
exists to find, and reports "no actionable improvement found". Compounded by H5 below: the
correction half of the gate fires in 2% of sessions, so the pattern half is effectively the
only gate there is.

**The one probe.** Against the server's `state.db`, for the configured horizon:

```
A = COUNT(DISTINCT session_id) over active role='tool' rows within horizon
    where _is_error_content(scrub_text(content), tool_name) is True
B = the same count, but simulating the live admission order: iterate rows
    ORDER BY timestamp DESC, stop at cross_session_max_rows, and spend a session
    slot on the first row of each new session up to cross_session_max_sessions
```

**Confirms it:** `B < A`. **Clears it:** `A == B` — the cap is not binding on the server's
data, and this is a desktop-only exposure.

**Minimal fix shape.** Spend a session slot only on rows that survive `_is_error_content`,
and log once when the session cap binds, as the row cap already does. Cost is unchanged: the
SQL `LIMIT` still bounds rows scanned. Both directions are testable on a synthetic db — a
chronic failure spread over 40 old sessions must be counted; a corpus of 100 error-free
recent sessions must not push the query past its row budget.

---

## H3 — `grounded` is measured and thrown away, so an unobserved fingerprint reads as "did not recur"

**What could be wrong.** `core.py:3778-3781` computes exactly the right fact — is the
proposal's `pattern_fingerprint` one of the fingerprints that were actually rendered to the
model — and stores it in `llm_meta` as a **metric**. Nothing gates on it. `_validate_proposal`
checks only the *shape* (`core.py:2316`, `[0-9a-f]{12}`). So a shape-valid fingerprint that
was never observed — invented, mistyped, or carried over from a stale journal entry — lands
in the ledger. `ledger.audit` then does `by_fingerprint.get(fingerprint)` → `None` →
`recurred = False`, and `recurred is False` is one of the conditions for the *positive*
verdict (`ledger.py:624`).

**Why this codebase produces it.** Defect 14 was this same inference in a different
direction: `recurred=False` on an empty window presented as "did not recur". The fix guards
the *empty window* case (`window_empty`, `ledger.py:602-609`) — a window with rows but no
matching fingerprint is not empty, so the guard does not apply, and absence is read as
silence again. A verdict reporting on something it cannot see.

**Consequence if true.** The ledger is the plugin's only feedback loop on whether its own
edits help, and this credits an ungrounded edit as working, permanently. Reading the verdict
chain closely bounds where the damage lands: the `working` branch also requires
`uses > 0 and usage_scope == "since_exact"` (`ledger.py:616-618`), so the false positive is
reachable only for a **skill** that is genuinely being used. An ungrounded fingerprint cannot
manufacture the negative verdict — absence yields `False`, and `did not help` needs `True`.

**A sharper finding fell out of checking that.** Because `uses` is hardcoded unavailable for
non-skills (`ledger.py:581-582` — the host exposes a usage counter only for skills), the
`working` branch is unreachable for a prompt note or a memory entry by construction. Their
recurrence can only ever produce `did not help` or nothing. On this install every applied
edit is a prompt note, and the audit says exactly that: 4 `unclear`, 2 `rolled back`, all six
rows `pattern_recurred=None, usage_scope=unavailable`. So for the edit kind that dominates in
practice, the feedback loop has no positive verdict available at all — it can report failure
or silence, never success. Whether that is a deliberate boundary or an oversight is not
something the code says; `ledger.py:581-582` justifies withholding *usage*, not withholding
*usefulness*. Worth a decision before anyone reads `unclear` as "no effect".
(Measured via `ledger.audit()` called directly, which supplies no pattern window; run
`/refine audit` on the server for the numbers on the real path.)

**Measured:** all 5 applied entries carry `grounded=True, fingerprint_offered=8`. The model
did use offered fingerprints on this sample. So the exposure is structural, not yet observed.

**The one probe.** A unit test, no host needed: build a journal entry whose
`pattern_fingerprint` is a valid 12-hex string absent from the supplied pattern window, run
`ledger.audit` with a non-empty pattern window, and read the row.
**Confirms it:** `pattern_recurred=False` and a verdict on the "working" branch.
**Clears it:** `pattern_recurred=None` with a verdict that says the fingerprint was not
checkable.

**Minimal fix shape.** Two candidates, and they are not equivalent. Either refuse the edit
when `grounded` is false (strongest, but it can reject an otherwise good edit for a typo), or
carry `grounded` into the ledger meta and make an ungrounded fingerprint yield
`recurred=None` with its own verdict string, the way `no recurrence window` already
distinguishes "unmeasured" from "silent". Prefer the second: it makes the failure
distinguishable in the audit instead of making it fatal at apply time.

The plumbing for the second already exists and is three small edits: `record_edit` receives
`llm_meta` today and can store `fingerprint_grounded` additively, exactly as it does
`reported_model` (`ledger.py:203-210`); `_merge_journal_stats` can read the same value from
`entry["llm_meta"]["grounded"]` (`ledger.py:387-404`); and the verdict chain withholds
recurrence when the stored value is explicitly `False`. Historical rows carry no such key, so
treat *missing* as grounded — only a positively-known-ungrounded fingerprint should change a
verdict, or every row already in the ledger silently re-labels itself.

---

## H4 — Three comparisons assume `messages.timestamp` is Unix seconds; nothing verifies it

**What could be wrong.** The cross-session horizon (`core.py:1329`, `time.time() - days*86400`),
the ledger's usage count (`ledger.py:272`, `timestamp > ?`) and the audit's recurrence test
(`ledger.py:587`, `last_ts > created`, where `created` is a journal `time.time()` value) all
compare a host-owned column against a plugin-owned clock. If the host ever stored
milliseconds or an ISO string, the horizon becomes inert (silently — everything passes) and
`recurred` becomes `True` for every pattern that exists at all.

**Why this codebase produces it.** It is the only assumption in the plugin that spans two
independent clocks and is never asserted anywhere.

**Measured, live install — this one is CLEARED:**

```
timestamp typeof=real  min=1780961151.13  max=1787950862.49  now=1787951102
```

Unix seconds, `real`, current. All three comparisons are sound on this host.

**The one probe, for the server.** `SELECT typeof(timestamp), MIN(timestamp), MAX(timestamp)
FROM messages;` compared with `time.time()`. One query closes all three call sites.
**Confirms it:** `typeof` is `text`, or `max` is ~1000x `time.time()`.
**Clears it:** the result above. Worth running once because it is one query and it retires an
assumption rather than an implementation.

---

## H5 — The correction path opens the gate on a single event, with no repetition requirement

**What could be wrong.** `has_signal` returns True on any non-empty `corrections` list, with
no count threshold — one user message classified as a correction is enough to authorize an
edit that permanently enters future context, in a plugin whose stated purpose is failures
that *repeat*. Defect 3 was the first-message false positive; this is the remaining
direction, and it was never tested as "does the gate stay closed when the evidence is thin".

**Measured, live corpus (170 sessions) — CLEARED as a false-open risk:**

```
sessions with >=1 correction: 4 of 170 (2%)   total corrections: 10
```

`_is_correction` is narrow enough in practice that the single-event gate almost never fires.

**But the measurement inverts into H2's argument.** If corrections contribute 2%, the pattern
path is effectively the only gate, and H2 says that path sees 12 of 66 error-bearing
sessions. Both halves of the gate are weak — one by measurement, one by construction. That is
the most likely reason this plugin proposes so little: **34 of 58 journal entries are
`llm_invocation_unavailable` and 12 are `no_op`; 5 edits landed in total.** The 34 are a
provider problem and are honestly journaled. The 12 deserve the H1/H2 probes before anyone
concludes there was nothing to find.

**The probe, if it is ever worth reopening:** count sessions with ≥1 correction and no
qualifying pattern, on the server. Confirms a real exposure only if that count is a
meaningful share of sessions.

---

## Checked during this pass and closed, with the reason

Recorded so the next pass does not re-spend the credits.

- **Skill rollback's exact-equality precondition is not a dead promise.** `rollback_skill`
  refuses unless `read_skill_content(name) == proposal["content"]` byte for byte
  (`journal.py:2793-2796`), while `is_reversible` promises reversibility from snapshot
  availability alone. That asymmetry is safe because an edit can only *reach* `applied` after
  `journal.target_matches_applied` proved the same byte equality against a real read-back
  (`core.py:4386`), and the staged path proves it again in `reconcile` (`journal.py:2628`).
  Host-side write normalisation would fail the edit at apply time, not silently at rollback
  time. No probe needed.
- **The daily budget cannot be raced.** `daily_limit_reached()` is read-then-act, but both the
  check and the apply sit inside one `journal.mutation_lock()` (`core.py:4042-4057`), and that
  lock is cross-process — socket lease plus `O_EXCL` claim file, no `fcntl` (`journal.py:446-1382`).
  Two processes cannot both pass the gate. The concurrency worry in AGENTS.md is about the
  ledger's read-modify-write, not this.
- **`has_signal` is called on the set the model actually sees.** `core.py:3573-3587` calls
  `prioritize_signal_patterns` first and gates on its truncated result, which is what the
  docstring claims. The order is right at that call site, and it is the only gate site.
- **Journal timestamps and the budget agree on UTC.** `count_today_applied` compares
  `datetime.now(timezone.utc).date()` against entry `ts` read back as UTC (`journal.py:1892-1904`).
  No local-time seam, so the 3-hour desktop offset cannot move a day boundary.

---

## What this pass did not touch

Live LLM proposer/reviewer calls, live rollback against the host, and crash injection remain
unexercised — same as both audits. Nothing above depends on them: H1, H2 and H4 are settled
by read-only queries, and H3 by a unit test.

---

## Appendix — the probes, verbatim

Read-only. Run from the plugin directory with the Hermes interpreter. Prints aggregate counts
only; no trajectory content is emitted, so the output is safe to paste into a report.

**H1 + H2 + H4 in one pass** (one full scan of `messages`):

```python
import json, sqlite3, sys, time
sys.path.insert(0, ".")
import config, core
from sanitization import scrub_text

conn = sqlite3.connect(f"file:{config.state_db_path()}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

# H4 — timestamp units
r = conn.execute("SELECT typeof(timestamp) t, MIN(timestamp) lo, MAX(timestamp) hi FROM messages").fetchone()
print(f"timestamp typeof={r['t']} min={r['lo']} max={r['hi']} now={time.time():.0f}")

# H1 — does the scrubber cost us the structured verdict?
rows = conn.execute("SELECT tool_name, content FROM messages WHERE role='tool' AND active=1").fetchall()
changed = jraw = broken = lost = f_e2o = f_o2e = 0
for row in rows:
    raw = str(row["content"] or "")
    if not raw:
        continue
    tool, scrubbed = str(row["tool_name"] or ""), scrub_text(raw)
    if raw == scrubbed:
        continue
    changed += 1
    try:
        json.loads(raw.strip()); parses = True; jraw += 1
    except Exception:
        parses = False
    if parses:
        try:
            json.loads(scrubbed.strip())
        except Exception:
            broken += 1
    if core._structured_error_status(raw) is not None and core._structured_error_status(scrubbed) is None:
        lost += 1
    e_raw = core._is_error_content(raw, tool_name=tool)
    e_scr = core._is_error_content(scrubbed, tool_name=tool)
    f_e2o += int(e_raw and not e_scr)
    f_o2e += int(e_scr and not e_raw)
print(f"all_tool_rows={len(rows)} scrub_changed={changed} json_parsed_raw={jraw} "
      f"json_broken_by_scrub={broken} structured_verdict_lost={lost} "
      f"classification_error_to_ok={f_e2o} classification_ok_to_error={f_o2e}")

# H2 — how many error-bearing sessions can the live query actually reach?
since = time.time() - config.cross_session_days() * 86400
win = conn.execute(
    "SELECT session_id, tool_name, content FROM messages "
    "WHERE role='tool' AND active=1 AND timestamp >= ? ORDER BY timestamp DESC", (since,)
).fetchall()
def is_err(row):
    return core._is_error_content(scrub_text(str(row["content"] or "")), tool_name=str(row["tool_name"] or ""))
A = {str(x["session_id"] or "") for x in win if is_err(x)}
cap, max_rows = config.cross_session_max_sessions(), config.cross_session_max_rows()
seen, B = set(), set()
for i, x in enumerate(win):
    if i >= max_rows:
        break
    sid = str(x["session_id"] or "")
    if sid and sid not in seen:
        if len(seen) >= cap:
            continue
        seen.add(sid)
    if sid in seen and is_err(x):
        B.add(sid)
print(f"window_rows={len(win)} max_rows={max_rows} session_cap={cap} "
      f"sessions_with_errors={len(A)} reachable_by_current_query={len(B)} slots_spent={len(seen)}")
conn.close()
```

**H1, the harm direction, with no database at all:**

```python
import sys; sys.path.insert(0, ".")
import core
from sanitization import scrub_text
raw = '{"success": false, "session_id": 918273645, "detail": "nope"}'
print(core._structured_error_status(raw), core._is_error_content(raw, tool_name="t"))
s = scrub_text(raw)
print(core._structured_error_status(s), core._is_error_content(s, tool_name="t"))
# observed: (True, True) then (None, False) — a genuine failure reads as a success
```

---

# Second pass — three questions after the server run

Server results read from `/home/ubuntu/refine-live-artifacts/phase-d/kiro-hypotheses/`
(`REPORT.md`, `h4_server_deep_probe.py`, `h4h5_server_probe.py`). Desktop measurements below
were re-run here against `%LOCALAPPDATA%\hermes\state.db` (15333 rows, 2694 active tool rows).

## Q1 — H4 split across hosts: is it server-only, or does the desktop probe not see it?

**Neither, and the distinction the question draws is the right one to insist on.** The answer
is a third option: **the code defect is on both hosts, identically; the poison is data, and
the data differs. The desktop probe was weak — but not in the direction that hid the server's
poison.** Three separate claims, each measured.

**1. The defect is in the code, and it is demonstrable on the clean desktop.** Nothing in
`config.py` touches timestamps; no configuration participates. What the three call sites share
is that they compare a host-owned column against a plugin-owned clock with no validation. I
built a throwaway db with three rows — one sane, one at `1.06e+305` (the value the server's
live db actually carries), one at `-7.56e+166` — and pushed it through the desktop's own code:

```
call site 1, cross-session horizon, days=1:
  ts=1.788e+09  in_1day_window=True   counted=1
  ts=1.060e+305 in_1day_window=False  counted=1     <-- admitted anyway
  patterns_returned=2   (a 1-day horizon should hold 1; the negative row is gone entirely)
call site 3, audit recurrence (last_ts > created), edit made now:
  ts=1.060e+305 recurred=True         <-- recurs against ANY edit, forever
  ts=1.788e+09  recurred=False
```

So `cross_session_days` is not a horizon for a future-dated row, and `recurred` is permanently
`True` for one. Same code, same platform where H4 was "cleared". Call site 2
(`ledger._count_uses_with_scope`) returned `(0, 'since_approx')` for both a now-baseline and a
year-3000 baseline in my synthetic setup, so **that site I did not manage to exercise** — the
usage matcher did not match my synthetic row, and I stopped rather than spend credits tuning a
fixture. Treat call site 2 as unmeasured, not as safe.

**2. The desktop data really is clean, and that part of the original verdict stands.** A
stronger census — per-row, not aggregate:

```
typeof census (per row):  typeof=real rows=15333        (one type, no mixed column)
sanity buckets (all rows): rows=15333 sane=15333 nulls=0 non_numeric=0 zeros=0
                           negative=0 future_gt_now_plus_1d=0
active tool rows:          2694  nulls=0 zeros=0 negative=0 future=0 sessions_affected=0
rowid/timestamp inversions: 44
```

Server, same column: `min=-7.56e+166`, `max=1.06e+305`, 288 future rows, 63 negative, 66/35
sessions affected. The two hosts differ in what has been written to that column, not in how
the plugin reads it. Nothing marks the desktop as structurally safer — it is one bad writer
away from the server's state, and nothing would announce the transition.

**3. The original probe was weak, in three named ways — none of which explains the split.**
`SELECT typeof(timestamp), MIN(timestamp), MAX(timestamp)` cannot see: NULLs (SQLite's
`MIN`/`MAX` skip them, and `row["timestamp"] or 0` then turns NULL into `0` — a value every
horizon excludes and every `last_ts > created` answers `False`, i.e. reports silence); a
**mixed-type** column, because a bare `typeof(col)` in an aggregate query reports one
arbitrary row; and `0` itself, which sits inside any sane min/max range. Extreme numeric
garbage, though, is exactly what `MIN`/`MAX` do catch — so the server's poison would have
shown on the desktop had it been there. The census above closes all three gaps and the desktop
is still clean on every one.

The 44 `rowid`/`timestamp` inversions are the one new thing that census found on the desktop.
Every window orders by `timestamp DESC`, so insert order and read order disagree for those
rows. 44 of 15333 is small and it is not the H4 defect; recorded, not chased.

**Consequence.** The exposure is not "does my host have bad rows today". It is that the plugin
treats a host-owned column as trusted input at three decision points, and the failure is
directional and permanent: a future-dated row is inside every horizon and recurs against every
edit forever, and a negative or zero one is invisible to every horizon and therefore reports
silence forever. Both are the project's standard shape — a comparison confidently reporting on
something it cannot see.

**The probe that proves this answer** (and settles it on any host, without needing bad data):

```python
# A. per-row census -- run on BOTH hosts, compare
#    SELECT typeof(timestamp), COUNT(*) FROM messages GROUP BY typeof(timestamp);
#    SELECT COUNT(*), SUM(timestamp IS NULL), SUM(timestamp = 0), SUM(timestamp < 0),
#           SUM(timestamp > strftime('%s','now') + 86400) FROM messages;
#    -> confirms whether a host's DATA is clean, per row, not per aggregate.
#
# B. code probe -- run on the CLEAN host, no live data needed
#    build a temp sqlite with the plugin's messages/sessions schema and three error
#    rows: ts=now-3600, ts=1.06e305, ts=-7.56e166; point config.state_db_path at it;
#    then assert:
#      core.collect_cross_session_patterns(days=1) returns ONLY the sane row
#      (last_ts or 0) > created is False for the future-dated row
#    -> confirms the defect is host-independent. It fails today on the desktop.
```

**Confirms "code defect on both hosts":** probe B fails on a host whose probe A is clean —
which is what happened here. **Would clear it:** probe B passing on the clean host, i.e. the
plugin already rejecting an out-of-range timestamp.

**Fix shape, if it is wanted** (not applied — the owner called it a separate decision): the
column is untrusted host input, so treat it the way row content is treated — one coercion at
the read boundary rather than three comparisons each guessing. A `_row_ts(value)` that returns
`None` for NULL, non-numeric, negative, zero, or beyond `now + small_skew`, and callers that
treat `None` as "no time" instead of `0`. That is one function and three call sites, and it
makes both directions testable on a synthetic db without waiting for a host to corrupt itself.
