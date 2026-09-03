# Spec — six verified findings from the live trace

Handoff spec for a junior model. Six items, all bug fixes. **No new capability.**

Baseline: `main` at `611b234` or later — read HEAD yourself. Suite is **1046 OK
(skipped=6)**; confirm that before you start and do not inherit the number.

Source: `G:\Claude\VERIFIED_FINDINGS_EVIDENCE.md`, produced by a live trace against
commit `611b234`. Every finding there arrived with a reproduction script and real
interpreter output. **I re-verified four of the six myself** before writing this, and
I say which below, because a plan built on an unchecked premise is worse than no
plan.

The trace also answered the question it was commissioned for: the plugin is **not**
writing junk, and it works correctly. None of the six items below is about that.
They are all defects in the machinery that reports, aggregates, or guards — which is
why they survived: they do not make anything crash.

---

## Non-negotiable rules (AGENTS.md governs)

1. **Python 3 standard library only.** No new dependencies. Item 4 is literally a
   guard for this rule; do not violate the rule while implementing its guard.
2. Suite from the repo root:
   `$env:PYTHONIOENCODING='utf-8'; python -m tests.run_tests`
   Reading the output is the evidence. **Do not use bare `python` if it resolves to
   a Windows Store stub** — check with `where.exe python`; the working interpreter is
   `C:\Users\relig\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe`.
3. **One commit per item**, message explains *why*. **Push after every commit.**
4. **Fail-first is mandatory.** Write the test, run it against the parent, **read**
   the failure, quote it in the commit message. The evidence file already contains a
   reproduction for five of six — reuse it, but run it yourself and quote your own
   output. A quoted result you did not produce is not evidence.
5. `git diff --check` clean and `py_compile` clean before each commit.
6. **New tests go in `tests/run_tests.py`.** It is the only file CI executes.
   `tests/test_usefulness.py`, `test_block_rule_fallback.py` and
   `test_proposer_status.py` are stale duplicates and are not imported by anything.
7. **Never let a mutate-test-restore sequence share one command with anything that
   can be interrupted.** That already left `install.py` broken mid-session once.
8. **Do not touch the live server.** No ssh, no gateway restart, no deploy. Another
   model may be tracing against it.
9. Commit author must be `263254659+Bergschloss@users.noreply.github.com`.
10. CI runs on every push (ubuntu + windows × 3.11 + 3.12). CI **cancels
    in-progress runs**, so confirm the run for your *final* SHA, not an earlier one.

---

## Order

Do them in this order. It is not arbitrary: items 1 and 4 harden the suite itself,
so doing them first means the later items are validated by a suite that can actually
detect loss.

1. Item 1 — the discovery floor counts classes, not tests
2. Item 4 — no automated guard for stdlib-only
3. Item 2 — `entry_ts` discarded
4. Item 5 — `model_substituted` dropped from the audit row
5. Item 6 — `rejected` conflates two causes
6. Item 3 — multiline traceback fingerprints diverge  ← hardest, do it last

---

## Item 1 — [09-01] the discovery floor counts classes, not tests

**File:** `tests/run_tests.py` (~line 19628)

**I verified this myself.** The comment and the assertion disagree:

```python
# A floor on the total so a broadly broken discovery cannot hide by
# keeping one class. 600 is deliberately well below today's 659+7 so
# normal test churn does not fail it; a catastrophic loss does.
self.assertGreaterEqual(len(discovered), 2)
```

`discovered` is a set of **class names**. There are 17 classes. So the floor the
comment describes — 600 test cases — is not enforced by anything; the code asserts
that at least 2 classes exist. A discovery collapse from 1046 tests to 10, spread
over two classes, passes.

This matters more than it looks: CI's own floor of 700 is the outer guard, and this
assertion is the inner one. Right now the inner guard is decorative.

**Fix:** keep the class assertion and add the one the comment describes.

```python
self.assertGreaterEqual(len(discovered), 2)
self.assertGreaterEqual(suite.countTestCases(), 600)
```

**Fail-first:** temporarily assert `countTestCases() >= 99999` to prove the new
assertion is live and reached, read the failure, then set it to 600. Do not commit
the temporary value. Better: write the test so it fails before the fix by asserting
on the *current* code path — if that is not possible, say so and use the temporary
probe, quoting its output.

**Do not** raise CI's floor of 700 in this commit. The floor guards against tests
vanishing; moving it while touching test discovery makes both changes unreviewable.

---

## Item 4 — [09-02] nothing stops a third-party import

**File:** `tests/run_tests.py`

Invariant 7 says stdlib only. The existing test
(`test_installed_tree_contains_all_imported_plugin_modules`) computes
`referenced & repo_owned`, which by construction can only ever contain the plugin's
own modules. `import requests` is not in `repo_owned`, so the intersection is empty
and the test stays green. Measured in the evidence file:

```
Referenced third-party libraries: {'requests', 'numpy', 'pydantic'}
Did line 22699 catch them? False
```

That test is not wrong — it answers a different question (did the installer copy
every module the plugin imports). Leave it alone and add a separate guard.

**Fix:** a new `unittest.TestCase` that walks the AST of every `*.py` in the repo
root and asserts each top-level import is stdlib, a repo module, or a known Hermes
host package. The evidence file contains a working version; use it as the starting
point but tighten it:

- `sys.stdlib_module_names` exists on 3.10+. CI runs 3.11 and 3.12, so it is safe,
  but assert it is non-empty rather than falling back to `frozenset()` silently — a
  guard that silently allows everything is worse than no guard.
- Cover `tests/*.py` too, not only the root. The suite is code that ships.
- The Hermes host allowlist must be **explicit and small**: `agent`, `gateway`,
  `hermes_cli`, `hermes_constants`, `tools`. Do not add anything else without
  saying why in the commit message.
- Relative imports (`node.level > 0`) are repo-internal by definition; skip them.
- The failure message must name the file, the line, and the offending module. A test
  that says only "forbidden import" costs the next person a search.

**Fail-first:** write a temporary file in a temp dir containing `import requests`,
point the walker at it, and prove it raises. Or add `import requests` to a scratch
copy — never to a real repo file. Read the failure and quote it.

**Both directions:** the guard must also *pass* on the real repo as it stands today,
and must not reject a legitimate stdlib import that happens to be rare
(`sqlite3`, `zipfile`, `importlib.util`, `contextvars` are all in use).

---

## Item 2 — [05-02] `entry_ts` is accepted and discarded

**File:** `ledger.py:176`

**I verified this myself.** `record_edit` takes `entry_ts: Optional[float] = None`
and line 176 never reads it:

```python
created_ts = previous.get("created_ts", now) if same_edit else now
```

The evidence file's reproduction, with `entry_ts = 1600000000.0` (year 2020):

```
Expected created_ts: 1600000000.0
Actual saved created_ts: 1788433456.3295336
```

**Consequence, and it is the reason this is worth fixing:** `created_ts` is what
`audit()` turns into `age_days`, and `age_days` is what decides whether a verdict is
`too early` or trustworthy. A backfilled or replayed entry is recorded as if it
happened now, so its age is wrong and its verdict can flip from "too early" to a
confident answer that the observation window never supported.

**Fix:**

```python
fallback_ts = entry_ts if entry_ts is not None else now
created_ts = previous.get("created_ts", fallback_ts) if same_edit else fallback_ts
```

**Check the whole function while you are there, but only for this:** find every
other place in `record_edit` that reaches for `now` where the caller supplied
`entry_ts`. If `updated_ts` has the same problem, that is part of this item; if it
deliberately means "when we last touched it", leave it and say so in the message.
Do not refactor anything else.

**Both directions:** an explicit `entry_ts` is honoured; `entry_ts=None` still
records `now`; and a second `record_edit` for the **same** edit preserves the
original `created_ts` rather than resetting it.

---

## Item 5 — [05-01] the model-substitution warning is dead code

**File:** `ledger.py` — `audit()` row construction, and `format_audit()` ~line 982

**I verified this myself.** `model_substituted` is computed (line 391), stored into
`meta` (line 444), and read by `format_audit`:

```python
if row.get("model_substituted"):
    lines.append("      ⚠ model substituted: ...")
```

But `audit()` never copies it from `meta` into `row`. Measured keys in the evidence
file confirm its absence, and `row.get("model_substituted")` is therefore always
`None`. The warning has never once been printed.

**Why it matters:** the warning exists because a verdict produced by a model other
than the configured target is not trustworthy. Suppressing that notice means the
operator reads an untrustworthy verdict as a trustworthy one — the audit is
*confidently* wrong, which is worse than silent.

**Fix:** carry it into the row.

```python
row["model_substituted"] = bool(meta.get("model_substituted"))
```

Put it in the same dict literal as the other fields rather than mutating afterwards,
so the row's shape is readable in one place.

**Fail-first:** build a stats entry with `model_substituted=True`, call `audit()`,
assert the key is present and true, and assert `format_audit()` output contains the
warning. Both assertions fail on the parent — quote them.

**Both directions:** an entry *without* substitution must not produce the warning.
A `False` that renders as a warning would be the same defect mirrored.

---

## Item 6 — [Audit conflation] `rejected` hides which thing rejected it

**File:** `ledger.py:673-675`

```python
elif outcome == "rejected":
    uses, usage_scope = None, "unavailable"
    verdict = "rejected"
```

The journal holds 46 `rejected` outcomes. Some are the plugin refusing weak content
(duplicate, over the 200-character ceiling); some are the host store being
unavailable or timing out. The operator sees one word for both and cannot tell
"refine did its job" from "the host was broken".

**Read this before you start — the obvious fix is a heuristic.** The evidence file
proposes sniffing `meta["error"]` for the substrings `store` and `unavailable`. That
works on today's strings and breaks the first time a message is reworded, and it
cannot distinguish an error whose text merely mentions a store. I have previously
recorded that a *proper* fix needs the cause in the durable record, because
`result_code` exists only on the in-memory response and `llm_meta` is frozen at
`journal.prepare()` time.

So do this in the order below and **stop after step 1 if step 2 turns out to need a
journal format change**:

1. **Find out what is actually in the record.** Read real `rejected` entries in
   `skill_stats.json` and in the journal fixtures in the suite. Report which fields
   are present on a store-unavailable rejection versus a content rejection. If a
   structured field already distinguishes them — a failure code, a marker string
   like `_MEMORY_STORE_UNAVAILABLE_MARKER` in `core.py`, anything — use that, and
   this item is a clean two-line fix.
2. **If and only if no structured field exists**, implement the string heuristic,
   but make it narrow and honest: match the actual marker constant the code writes,
   not the generic words `store` or `unavailable`. Name the verdict
   `store unavailable` and add a comment saying it is inferred from the message
   because the cause is not in the durable record.
3. **If neither is possible**, do not guess. Report that the durable record cannot
   answer the question, leave the code as it is, and stop. That is a legitimate
   outcome and is already documented as a known limitation.

Whatever you do, do **not** change the journal format in this item. That is a
separate decision with its own risks.

**Both directions:** a content rejection must still read `rejected`. Only a
store-unavailable rejection may read differently.

---

## Item 3 — [06-03] multiline tracebacks never aggregate

**File:** `patterns.py:247-265`

This is the one with real behavioural consequence, and the one most likely to break
something else. Do it last, with the most care.

The terminal-line scan walks the block backwards and takes the first non-wrapper
line as the exception line. When an exception's message spans several lines, the last
line is a continuation (`context details...`), `_is_python_exception_line` returns
`False` for it, so `exception_line` stays `None`, the text is never replaced by the
exception line, and **the stack frames stay in the fingerprinted text**. Frames
contain file and function names, so two call sites of the same error fingerprint
differently.

Measured in the evidence file:

```
=== SINGLE LINE TRACEBACK FINGERPRINTS ===
Callsite A: 34d3a78fbaaa   Callsite B: 34d3a78fbaaa   aggregate? True

=== MULTILINE TRACEBACK FINGERPRINTS ===
Callsite A: dcddeb59ae68   Callsite B: 18a40157881d   aggregate? False
```

**Consequence:** the signal gate needs recurrence ≥ 2 on one fingerprint. A
multiline exception raised from two places never reaches 2, so refine never sees a
whole class of real, repeating failures. This is the finding that costs the plugin
actual capability.

**Before you write anything, read `AGENTS.md` on normalization.** It has two
opposing requirements: volatile detail must collapse while genuinely different
errors stay apart. Changing one rule usually breaks the other direction, and there
are tests for both. **Both must still pass.** Run the full suite, not just your new
tests, and if any existing normalization test breaks, that is not noise — it means
your fix collapsed something it should not have.

**Fix direction** (the evidence file's version is a reasonable start): find the
exception line by scanning for the *first* line that is an exception line and is not
indented, then join it with the continuation lines that follow it. The joined message
is the fingerprint input; the frames are dropped as they already are for single-line
exceptions.

Watch for:

- **Chained exceptions.** `During handling of the above exception, another occurred`
  and `The above exception was the direct cause` — there are already wrapper
  patterns for these. Which exception should win, the first or the last? Decide,
  state the decision in the commit message, and test it.
- **A message that legitimately contains a path or an id.** Those must still
  collapse, or you have traded one aggregation failure for another.
- **Indented continuation vs an indented nested frame.** `line.startswith("  ")` is
  doing real work in the proposed fix; make sure a `  File "..."` frame cannot be
  mistaken for a continuation line.

**Tests — at least four:**

1. The exact reproduction from the evidence file: two call sites, multiline
   exception, same fingerprint. Fails on the parent — quote it.
2. Single-line exceptions from two call sites still aggregate (regression guard on
   the behaviour that already works).
3. Two **genuinely different** multiline exceptions still fingerprint differently.
   This is the opposing direction and it is the one that catches an over-collapsing
   fix.
4. A chained multiline traceback, asserting whichever behaviour you decided.

---

## Stop condition

Done when, each read rather than assumed:

- Six commits (or five plus a written report for item 6 step 3), pushed, each with
  its own fail-first output quoted.
- Suite green after every commit, count stated each time. Baseline 1046.
- `py_compile` clean; every name you removed grepped for live references.
- CI green **4/4 on the final SHA**, confirmed via
  `gh run list --limit 1 --json databaseId` then `gh run view <id> --json jobs`.
- A closing note, one line each: does the discovery floor now catch a collapse; is a
  third-party import now rejected; does `age_days` follow `entry_ts`; does the
  substitution warning appear; can an operator tell the two rejection causes apart;
  do multiline tracebacks from two call sites aggregate. Plus what remains
  unverified.

**Stop and report instead of improvising if:** item 3's fix makes any existing
normalization test fail and you cannot satisfy both directions; item 6 needs a
journal format change; item 4's guard rejects something the repo legitimately
imports; or any item would need `install.py`, `notify.py`, or the live server.

## Out of scope

- Raising the CI floor, or extending CI in any way.
- The known-accepted limitations: refine cannot `replace` a memory entry; a bare
  dotted host in a verb frame is a declared limit; macOS is unverified; the Windows
  installer has not been exercised end to end.
- Deleting the three stale test files. Recorded, decided separately.
- Any refactor the six findings do not name.
