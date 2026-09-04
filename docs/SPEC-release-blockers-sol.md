# Spec — release blockers from the Sol audit

**The release is on hold until items 1–3 are closed.** Item 4 is the version bump and is done last,
after them.

Baseline: read HEAD yourself. Suite at the time of writing: **1084 OK (skipped=6)** — confirm it and
do not inherit the number. Any doc still saying 1046 is stale.

Source: Sol's read-only audit of `v0.12.0..HEAD`. Fable produced nothing (it crashed). I verified
every item below myself, by execution or by reading the exact lines, and I say which.

---

## Rules (AGENTS.md governs)

1. **Python 3 standard library only.**
2. Suite from the repo root: `$env:PYTHONIOENCODING='utf-8'; python -m tests.run_tests`.
   If bare `python` resolves to a Windows Store stub, use
   `C:\Users\relig\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe`.
3. **One commit per item.** Push after each. Author
   `263254659+Bergschloss@users.noreply.github.com`.
4. **Fail-first is mandatory.** Write the test, run it against the parent, **read** the failure,
   quote it. For items 1 and 2 the reproduction is described; produce your own output.
5. `git diff --check` clean and `py_compile` clean before each commit.
6. New tests go in `tests/run_tests.py` — the only file CI executes.
7. **Never let a mutate-test-restore sequence share one command with anything that can abort.**
8. **Do not touch the live server.** No ssh, no deploy, no gateway restart.
9. CI cancels in-progress runs; confirm the run for your **final** SHA.

---

## Item 1 — Critical: `install.sh` cannot actually restore the host

**File:** `install.sh`

**I verified this by reading the exact lines.** Three separate faults compound into one:

```bash
restore() {
    for f in "${TOUCHED_FILES[@]}"; do
        if [ -f "$BACKUP_DIR/$f" ]; then
            cp -p "$BACKUP_DIR/$f" "$HERMES_SRC/$f" || ok=0
        fi
    done
    ...
    say "RESTORE FAILED; recovery copy remains at $BACKUP_DIR" >&2
}
trap 'rm -rf "$BACKUP_DIR"' EXIT
```

- **Files the patch CREATED are never removed.** The backup loop only copies files that existed
  before patching, so `tests/agent/test_plugin_invocation_route.py` and anything else the patch adds
  has no backup entry, and `restore()` skips what it cannot find. A "restored" tree still carries
  files the patch introduced.
- **The git index is not restored.** `cp -p` puts file contents back but leaves whatever `git apply`
  staged. The tree can read as clean-but-staged, or dirty for reasons the user did not cause.
- **The recovery copy is deleted unconditionally.** `trap ... EXIT` removes `$BACKUP_DIR` on every
  exit path, including the one that just printed *"recovery copy remains at $BACKUP_DIR"*. That
  message is false at the moment it matters most — a failed restore. This is the worst part: it
  sends the user to a directory that the script deletes on its way out.

**Fix:**

- Record which of `TOUCHED_FILES` did **not** exist before patching, and have `restore()` delete
  exactly those. Nothing else — never delete a file the installer did not create.
- Restore the index for the touched paths (`git reset -q -- <paths>` scoped to them, or reset only
  what was staged). Do not run a bare `git reset`.
- Make the trap conditional: keep `$BACKUP_DIR` when a restore failed, remove it only on a clean
  path. Simplest correct shape is a flag the restore sets on failure and the trap reads.
- The message must match reality in both directions: say "removed" when it is removed, and give the
  path only when the path will still be there.

**Fail-first:** build the fake checkout the existing `InstallScriptTests` already builds, apply, then
force a verification failure, and assert (a) a created file is gone after restore, (b) the index is
clean, (c) `$BACKUP_DIR` still exists after a *failed* restore and is gone after a clean run.

**Both directions:** a successful install must still clean up its backup, and a restore must never
delete a file that existed before the install.

---

## Item 2 — High: a database failure is journaled as "nothing to propose"

**Files:** `core.py`

**I verified the return paths by reading `collect_cross_session_patterns` (from line 1688).** Both
the "disabled" and the "unavailable" branch return `[]` when `strict` is falsy:

```python
if not config.cross_session_enabled():
    if strict: raise IOError("Cross-session pattern collection is disabled")
    return []
connection = _open_db()
if not connection:
    if strict: raise IOError("Cross-session database is unavailable")
    return []
```

The call sites are line 2530 and line 4717. Sol reports that the main proposal path (4717) does not
pass `strict=True`, so an unreadable database is indistinguishable from a quiet window and the pass
journals `no_signal` / `no_op`.

This is the failure mode AGENTS.md names as the worst this plugin has: *"Any new failure must be
distinguishable in the journal from 'nothing to propose'."* An unreadable database is the exact
example it gives.

Sol also reports two more paths that skip the journal entirely: `session_unknown` (~line 4450) and
`daily_limit_reached` (~line 4533). A budget refusal and an unknown session are both legitimate
outcomes, but if they leave no journal row then afterwards they are indistinguishable from a pass
that never ran.

**Fix, in this order:**

1. **Start by reading the three call sites and reporting what you find**, because the fix depends on
   it. Does 4717 pass `strict`? Do 4450 and 4533 really return before any `journal.record`? Quote
   the lines. If Sol's reading is wrong on any of them, say so — that is a valid outcome and worth
   more than a fix built on a misreading.
2. Make the distinction reachable. Either pass `strict=True` at the main call site and catch the
   `IOError` into a distinct outcome, or have the collector report *why* it returned nothing through
   an out-parameter the way `rows_truncated` and `suppressed_out` already do. **Prefer the
   out-parameter**: the function already has that pattern for exactly this reason, and raising
   changes control flow on a path that currently cannot fail.
3. Journal `session_unknown` and `daily_limit_reached` as their own outcomes. Check first whether
   either already consumes a budget slot — if one does and is not journaled, that is a second defect
   and it belongs in this item.
4. **Do not change the journal schema.** Add outcome values, not fields.

**Fail-first:** patch `_open_db` to return `None`, run a pass, and assert the journal entry is
distinguishable from a genuine no-signal pass. On the parent both are `no_op` — quote that.

**Both directions:** a genuinely quiet window must still journal as no-signal, not as a failure.
Manufacturing a failure outcome for a quiet window would be the same defect mirrored.

---

## Item 3 — High: normalization fabricates recurrence

**File:** `patterns.py` (the number rules, ~line 148-156)

**I verified this by execution, not reading.** Sol rated it Medium; the measurement says High,
because it manufactures the very recurrence the signal gate depends on:

```
case                         collapsed   expected
exit code 1 vs 127           True        False      DEFECT
port 22 vs 443               True        False      DEFECT
tcp :22 vs :443              True        False      DEFECT
HTTP 404 vs 500              False       False      correct
ids /users/8821 vs 9134      True        True       correct
timeout 10s vs 15s           True        True       correct
```

Normalized forms:

```
'command failed with exit code 1'   -> 'command failed with exit code n'
'command failed with exit code 127' -> 'command failed with exit code n'
'connection refused on port 22'     -> 'connection refused on port n'
'connection refused on port 443'    -> 'connection refused on port n'
```

`exit code 127` is "command not found"; `exit code 1` is any failure at all. They are different
failures with different lessons. Two unrelated failures now count as one fingerprint seen twice —
which is exactly the threshold that opens the gate. The plugin can then write a confident lesson
about a recurrence that never happened.

**The mechanism to copy already exists in the same file.** HTTP statuses are preserved as
`httpstatus404` / `httpstatus500` rather than collapsed to `N`. Exit codes and ports need the same
treatment, before the general `\b\d+\b -> N` rule reaches them.

**Fix direction:** add rules that preserve the semantically meaningful number, ahead of the general
integer rule:

- `exit code <n>` / `exit status <n>` / `exitcode=<n>` → keep the value, e.g. `exitcode127`
- `port <n>` and `:<port>` in a host:port position → keep the value
- Consider signal numbers (`killed by signal 9` vs `15`) — check whether they collapse too, and say
  either way.

**Read `AGENTS.md` on normalization before you write anything.** Its two requirements pull against
each other, and there are tests for both directions which **must both still pass**. Run the full
suite, not just your new tests. If an existing normalization test breaks, that is not noise — it
means the new rule stopped something collapsing that should collapse.

**Watch for:** a port number inside a path or an id (`/v2/8080/items`) must NOT be preserved as a
port; a timeout duration must still collapse; and a number that is genuinely incidental must not be
promoted just because the word "port" appears nearby.

**Tests — at least five:** the three defect cases above, plus the two correct cases as regression
guards, plus one where a number near the word "port" is incidental and must still collapse.

**Note on collision:** `patterns.py` has had five commits recently and the multiline-traceback work
landed there. **Pull immediately before starting**, and if a merge is needed, stop and report rather
than resolving blind.

---

## Item 4 — Medium: the release version is a lie. Do this LAST.

`plugin.yaml` says `0.13.0`. HEAD is **30 commits** past the `v0.13.0` tag, and `core.py`,
`install.py`, `ledger.py`, `llm.py` and `patterns.py` all differ from what that tag contains. An
installed `0.13.0` and a fresh `0.13.0` are not the same software, which makes every field report
that quotes a version useless.

**Do this only after items 1–3 are green**, in one commit: bump `plugin.yaml` to `0.14.0`, tag
`v0.14.0`, push the tag. Do not bump it earlier — a version that moves while blockers are open is
the same lie with a different number.

---

## What Sol verified as sound — do not re-audit

- `state.db` is opened `mode=ro`.
- **No unscrubbed path out of the database was found.** That was the top question and it is answered.
- Daily budget and ledger are protected by a runtime lock, and the concurrency tests genuinely start
  threads.
- An approval-gated edit correctly stays `pending_approval`.
- No new runtime dependencies.

## Known and out of scope

- **The local desktop Hermes is at `18a76be12`, untagged — not v2026.8.31**, and no bundled patch
  applies to it. So the invocation-bound route is unavailable *there* and the proposal path stops at
  `llm_invocation_unavailable`. On the **server** the v2026.8.31 patch applies cleanly to
  `29112bef09` and a full clean install was verified end to end. Do not rebase a patch onto
  `18a76be12` in this batch — that is a separate decision about which Hermes builds we support.
- Refine cannot `replace` a memory entry; the audit verdict cannot distinguish store-unavailable from
  content-rejected. Both need a durable journal format change.
- Actual usefulness on real dialogues is still unproven — there is no temporal holdout showing fewer
  repeat failures. A corpus run is prepared separately for that and is not part of this spec.
- macOS unverified.

## Stop condition

- Three commits for items 1–3, then the version commit and tag.
- Suite green after each, count stated. Baseline 1084.
- CI green 4/4 on the final SHA.
- One closing line each: can `install.sh` now restore a host including files it created and its
  index; is a database failure now distinguishable from a quiet window in the journal; do
  `exit code 1` and `exit code 127` now have different fingerprints; and what remains unverified.

**Stop and report instead of improvising if:** item 3 breaks an existing normalization test in the
opposite direction; item 2 turns out to need a journal schema change; item 1 cannot restore the index
without a broader `git reset`; or `patterns.py` needs a merge.
