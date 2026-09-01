# Spec — low-risk audit fixes (dead code, duplication, key drift, weak tests)

Handoff spec for a junior model. Every item below is **confirmed on the current
code**, with exact locations and both directions to assert. None of these change
runtime behaviour of a working path; they remove traps and false-green tests. Do
them as **separate commits**, one logical item each.

Baseline: `HEAD == origin/main` (currently `ea8e914` or later — read it yourself).
Suite at baseline: run `$env:PYTHONIOENCODING='utf-8'; python -m tests.run_tests`
from the repo root and record the real count and `OK`. Do not inherit a number.

---

## Non-negotiable rules (from AGENTS.md — these govern)

1. **Python 3 standard library only.** No new dependencies.
2. Run the suite from the repo root: `$env:PYTHONIOENCODING='utf-8'; python -m tests.run_tests`.
   A command running is not evidence; **reading its output is**.
3. Work on `main`, one commit per item, author
   `263254659+Bergschloss@users.noreply.github.com`. Push after every commit.
4. Do NOT commit `__pycache__` or scratch files.
5. For any test you add or change: run it against the **parent** commit first,
   read the failure, and only then apply the fix. State the observed failure in
   the commit message. Do not assert a fail-first you did not see.
6. `git diff --check` clean, `python -m py_compile` clean on every file you touch,
   before each commit.
7. **Server format note:** the deployed plugin lives at `~/.hermes/plugins/refine`
   as a plain git checkout that is `git pull`ed. Nothing here changes that. Do not
   add build steps, packaging, or import-path changes — the files must keep running
   as bare modules on the server exactly as they do now.
8. If a probe does not reproduce the defect as described, **change no code and
   report the mismatch.** A finding that has moved is a result.

---

## Item 1 — remove the duplicate `prompt_notes_max_chars` (audit 07-03 / 11-05)

**File:** `config.py`

`prompt_notes_max_chars()` is defined **twice**, at line ~658 and again at line
~677, with identical bodies. Python keeps the second; editing the first is a
silent no-op. Confirmed:

```
def prompt_notes_max_chars() -> int:   # ~658
    return get_int("prompt_notes_max_chars", 600, min_val=1)
...
def prompt_notes_max_chars() -> int:   # ~677  <- this one wins
    return get_int("prompt_notes_max_chars", 600, min_val=1)
```

**Fix:** delete the **second** definition (~677-680), keeping the first so the
function stays where a reader expects it near the other prompt-note config. Verify
the remaining one is the first by source order.

**Test (add to the suite):** an AST test that no top-level function name in
`config.py` is defined twice.

```python
def test_config_has_no_duplicate_function_definitions(self):
    import ast, pathlib
    src = pathlib.Path(config.__file__).read_text(encoding="utf-8")
    names = [n.name for n in ast.parse(src).body
             if isinstance(n, ast.FunctionDef)]
    dupes = sorted({n for n in names if names.count(n) > 1})
    self.assertEqual(dupes, [], f"duplicate defs: {dupes}")
```

**Fail-first:** on the parent this test fails with
`duplicate defs: ['prompt_notes_max_chars']`. Read it, then delete the dupe.

---

## Item 2 — recurrence-horizon key drift: README says one key, code reads another (audit 07-02 / 11-04)

**Files:** `config.py`, `README.md`

`README.md` (~line 593) documents the config key as `refine.recurrence_horizon_days`.
`config.audit_recurrence_horizon_days()` (~line 663) reads **only**
`audit_recurrence_horizon_days`. An operator who follows the README sets a key the
code ignores, and silently gets the default 3.

**Fix (accept the documented alias, prefer the explicit key):**

```python
def audit_recurrence_horizon_days() -> int:
    """Days of post-edit silence after which 'no recurrence' means something."""
    entry = _get_refine_entry()
    if "audit_recurrence_horizon_days" in entry:
        return get_int("audit_recurrence_horizon_days", 3, min_val=1)
    return get_int("recurrence_horizon_days", 3, min_val=1)
```

Check the exact accessor name for the refine config dict already used elsewhere in
`config.py` (it may be `_get_refine_entry` or similar) and reuse it — do not invent
a new one. If no such helper exists, read both keys through the existing `get_int`
mechanism the file already uses.

Also add the key to the README config table so the documented name and a real
accessor agree. Keep the prose that already explains the horizon.

**Tests:**

```python
def test_recurrence_horizon_accepts_documented_alias(self):
    with mock.patch("config._get_refine_entry",
                    return_value={"recurrence_horizon_days": 10}):
        self.assertEqual(config.audit_recurrence_horizon_days(), 10)

def test_recurrence_horizon_explicit_key_takes_precedence(self):
    with mock.patch("config._get_refine_entry",
                    return_value={"audit_recurrence_horizon_days": 14,
                                  "recurrence_horizon_days": 10}):
        self.assertEqual(config.audit_recurrence_horizon_days(), 14)
```

**Fail-first:** the alias test fails on the parent (`3 != 10`). Confirm the exact
patch target matches how other config tests in the suite mock the refine entry —
grep the suite for how `audit_recurrence_horizon_days` or `_get_refine_entry` is
already tested and match that style, or the mock will not bite.

---

## Item 3 — delete dead constant `_TRACEBACK_MARKERS` (audit 06-05)

**File:** `patterns.py` (~line 155)

`_TRACEBACK_MARKERS = ("Traceback (most recent call last)", 'File "', "  at ")` is
defined and never read — the module moved to regex header detection. Confirm with
a repo search that the only occurrence is the definition:

```
grep -rn "_TRACEBACK_MARKERS" .   # must show exactly one line, the definition
```

**Fix:** delete the line. Do not touch the regexes below it.

**Verification:** the full suite stays green at the same count. No new test needed
for a pure deletion, but run the suite and read it.

---

## Item 4 — delete dead constant `_COMPAT_ASCII_BLOCK` (audit 08-05)

**File:** `sanitization.py` (~line 165)

`_COMPAT_ASCII_BLOCK` is a leftover from a refactor; `_normalize_compatibility_forms`
now uses a direct codepoint range check and never reads it. Confirm it is unused:

```
grep -rn "_COMPAT_ASCII_BLOCK" .   # only the definition
```

**Fix:** delete the constant **and** its now-orphaned explanatory comment block
directly above it (the comment describes the constant, so leaving it lies about
code that no longer exists). Do NOT touch `_normalize_compatibility_forms` or its
own comment.

**Verification:** full suite green, and specifically the fullwidth/compatibility
scrub tests still pass (grep the suite for `fullwidth` and run those by name).

---

## Item 5 — the trace-journal test asserts nothing (audit 09-04)

**File:** `tests/run_tests.py`, `test_trace_does_not_mutate_journal` (~line 17544)

The test ends in `self.assertTrue(True)`. Its stated purpose is that `emit_trace`
never writes to the mutation journal — but it verifies nothing. If a regression
made `emit_trace` append to the journal, this test would stay green.

**Fix:** make it assert the real invariant — the journal is unchanged across an
`emit_trace` call.

```python
def test_trace_does_not_mutate_journal(self):
    from trace import build_trace, emit_trace, finalize_trace
    import journal
    before = journal.read_journal()
    t = build_trace(session_id="s", source="hook",
                    operation="refine_run", route_state="bound")
    emit_trace(finalize_trace(t, result_code="success"))
    self.assertEqual(journal.read_journal(), before)
```

Check the real journal read API name — the suite already reads the journal
elsewhere; grep for `read_journal`/`entries(`/`_load_entries` and use whatever the
rest of the suite uses. If reading requires a configured journal dir, follow the
same fixture setup the neighbouring journal tests use (do NOT touch the real
`~/.hermes` journal — tests use a temp dir).

**Fail-first:** this one is subtle — the strengthened test should **pass** both
before and after (emit_trace already doesn't write). That is fine: this is a
test-quality fix, not a bug fix. State plainly in the commit that the production
behaviour was already correct and the change removes a vacuous assertion, so the
test can now catch a future regression. Do not fake a fail-first.

---

## Item 6 — the credential-in-trace test uses no credentials (audit 09-05)

**File:** `tests/run_tests.py`, `test_trace_no_raw_identity_in_output` (~line 17558)

The test builds a trace with `provider="openai", model="gpt-4"` — no secret — then
loops asserting no field starts with `sk-`/`Bearer `. Nothing secret was ever put
in, so the scrub path is never exercised.

**Fix:** feed a value that *looks* like a credential into a field that gets
scrubbed at the trace boundary, and assert the raw secret does not survive in the
emitted line. Use the boundary function the trace module actually applies — grep
`trace.py` for where it calls `scrub_text`/`_boundary` and assert against that.

```python
def test_trace_no_raw_identity_in_output(self):
    import trace as trace_mod
    secret = "sk-" + "A" * 32
    # A value shaped like a credential, through the same boundary emit_trace uses.
    scrubbed = trace_mod._boundary(secret)   # confirm the real fn name in trace.py
    self.assertNotIn("A" * 32, scrubbed)
    self.assertIn("[REDACTED]", scrubbed)
```

If `trace.py` exposes no single boundary helper, build a trace whose scrubbed field
carries the secret and assert the secret is absent from the final emitted string.
**Confirm the actual API before writing** — do not assume `_boundary` exists; the
audit named it but you must verify.

**Fail-first:** replace the assertion, run against the parent. If the parent's
trace boundary already scrubs `sk-...`, the test passes both sides — say so, same
as Item 5 (this hardens a vacuous test). If it does NOT scrub, you found a real
leak: **stop and report it**, do not weaken the test to pass.

---

## Order and stop condition

Do them 1 → 6. Items 1–4 are independent; 5–6 are test-only.

Done when, each read not presumed:
- All six committed separately, pushed, author correct.
- Suite green after each, count stated (baseline + any new tests).
- `py_compile` clean, `git diff --check` clean, no `__pycache__` committed.
- CI green 4/4 on the final pushed SHA. Pushing again before the previous run
  finishes cancels it — if you push several quickly, confirm the final SHA is green.
- Final note separating: which items were bug fixes (2, maybe 6) vs pure
  hygiene/test-hardening (1, 3, 4, 5), and for 5/6 whether production behaviour was
  already correct.

## Out of scope — do NOT touch

- `sanitization.py` credential patterns, `_normalize_compatibility_forms` logic.
- `core.py` `_CONTEXT_*`, `_memory_host_reference`, `_extract_binaries`,
  `_journal_nonmutation`, timeouts, `llm.py` `_overview_text` — all recently fixed.
- Anything involving the invocation route, provider, or model selection.
- The server's `~/.hermes/config.yaml`. Host config is not this repo's concern.
