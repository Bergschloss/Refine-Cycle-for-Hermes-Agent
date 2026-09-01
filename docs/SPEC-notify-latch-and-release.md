# Spec — fix the notification failure latch, two doc contradictions, ship v0.12.0

Handoff spec for a junior model. Five items, ordered. Only F1 changes behaviour; F2 is the
same file and the same concern; F3/F4 remove contradictions between the docs and the live
system; F5 releases.

**Explicitly out of scope** (asked for and declined — do not touch): test-infrastructure
findings 09-01/09-02, the `_refine_once` length, the test-file size, and anything in the
`docs/FINDING-*.md` files. Those are recorded decisions, not open work.

Baseline: `main`, currently `7e37fbf` — read HEAD yourself. Suite is **872 OK (skipped=6)**;
confirm that before you start, and do not inherit the number.

---

## Non-negotiable rules (AGENTS.md — these govern)

1. **Python 3 standard library only.** No new dependencies.
2. Run the suite from the repo root: `$env:PYTHONIOENCODING='utf-8'; python -m tests.run_tests`.
   Reading the output is the evidence. A command that ran is not evidence.
3. **One commit per item.** The message explains *why*. Author is already configured — commit
   normally, never touch git config. **Push after every commit.**
4. No `__pycache__`, no scratch files. Delete probes before committing.
5. **Fail-first for F1:** write the test, run it against the parent, **read** the failure, quote
   it in the commit message. F2's test may pass on the parent — if so, say so and label it
   hardening. Do not fake a fail-first.
6. `git diff --check` clean and `python -m py_compile <file>` clean before each commit.
7. **Server format:** the plugin runs as **bare Python modules** from a `git pull`ed checkout.
   No build steps, no packaging, no import-path changes. Do not SSH anywhere; local repo task.
8. `notify()` must **never raise** and must **never block refine**. Every change below keeps
   that: a notification is cosmetic, a refine outcome is not.

---

## F1 — CRITICAL: the failure latch silences every later failure

**File:** `notify.py`

**Current code:**

```python
_SEND_FAILURE_LOGGED = False
_SEND_FAILURE_LOCK = threading.Lock()


def _report_send_failure_once(target: str, detail: object) -> None:
    global _SEND_FAILURE_LOGGED
    with _SEND_FAILURE_LOCK:
        if _SEND_FAILURE_LOGGED:
            logger.debug(...)
            return
        _SEND_FAILURE_LOGGED = True
    logger.warning(...)
```

**The defect.** One process-wide boolean. The first undeliverable notification sets it, and
from then on **every other delivery failure in the process is debug-only** — including a
different target failing for a different reason. The gateway runs for weeks, so "once per
process" is in practice "once, ever".

This recreates the exact failure mode the WARNING was added to remove. AGENTS.md names
silent-forever as this plugin's default way of failing, and this module already shipped one
invisible failure (every notification failing to a bare platform name, logged at debug,
noticed only by driving the live path).

**Fix — throttle per cause, with a re-arm window:**

```python
# Failure reporting is throttled per CAUSE, not once per process. A single
# process-wide latch meant the first undeliverable notification silenced every
# later one, including a different target failing for a different reason, for the
# life of a gateway that runs for weeks. Keyed by (target, detail) so a new cause
# always speaks up, and re-armed after a window so a persistent misconfiguration
# resurfaces instead of scrolling away on day one. Identical failures stay at
# debug in between, which is what keeps an operator reading the log.
_FAILURE_REPEAT_SECONDS = 3600.0
_SEND_FAILURE_REPORTED: dict = {}
_SEND_FAILURE_LOCK = threading.Lock()
```

and the reporter becomes:

```python
def _report_send_failure(target: str, detail: object) -> None:
    key = (target, str(detail))
    now = time.monotonic()
    with _SEND_FAILURE_LOCK:
        last = _SEND_FAILURE_REPORTED.get(key)
        if last is not None and (now - last) < _FAILURE_REPEAT_SECONDS:
            logger.debug("refine notify: send to %r failed again (%s)", target, detail)
            return
        _SEND_FAILURE_REPORTED[key] = now
    logger.warning(...)   # see F2 for the text
```

Details that matter:

- `import time` at the top — check whether it is already imported before adding it.
- Use `time.monotonic()`, not `time.time()`: this is an elapsed-interval check and must not
  be skewed by a clock adjustment.
- **Rename** `_report_send_failure_once` → `_report_send_failure`; the old name now describes
  behaviour the function no longer has. Update **all** call sites — grep for it, there are
  several in `notify()` and in the worker.
- Keep the whole read-modify-write inside the lock, and do the `logger.warning` **outside**
  it. `on_session_end` starts a thread per session and the gateway runs several channels at
  once, so two failures can arrive together; holding a lock across logging is how that turns
  into a stall.
- Bound the dict. Keys are `(target, detail)` and `detail` is a short string like `exit 1`, so
  growth is slow, but a rotating target could still accumulate. Either cap it (drop the oldest
  entry past, say, 64) or purge entries older than the window when you insert. State which you
  chose and why in the commit.

**Tests — both directions:**

- `test_a_new_failure_cause_is_not_silenced_by_an_earlier_one` — report cause A, then cause B
  (different target **and** separately a different detail for the same target); assert **two**
  WARNINGs. Use `assertLogs`. **Fails on the parent** with one WARNING — that is the fail-first
  to quote.
- `test_identical_failures_are_throttled` — the same `(target, detail)` twice in a row yields
  one WARNING and one debug.
- `test_a_persistent_failure_is_reported_again_after_the_window` — patch
  `_FAILURE_REPEAT_SECONDS` to something small (or monkeypatch `time.monotonic`), assert the
  same cause warns again after it elapses. Do **not** `time.sleep(3600)`.
- `test_notify_still_never_raises_and_never_blocks` — the existing guarantees must survive; if
  there are already tests for those, run them by name and say they passed rather than adding
  duplicates.

Reset the new module state in the tests' `setUp`/`tearDown` the way the suite already resets
the other process-global variables — a leaked dict entry would make a later test's assertion
depend on run order.

---

## F2 — the warning text diagnoses the wrong cause

**File:** `notify.py`, same function as F1. Do it in the F1 commit **or** its own — your call,
but if separate, F1 first.

**The defect.** The message is fixed text that always blames the bare-platform-name case:

> "A bare platform name only routes when that platform has a home channel..."

That is right for one cause and wrong for the other. When there is **no active chat and no
configured target**, the operator configured nothing at all — telling them to go check home
channels sends them after the wrong thing. The call site for that case passes the literal
target `"(none)"`.

**Fix.** Choose the remedy from the cause. Two branches:

- **No address at all** (the `"(none)"` sentinel): say that there was no active chat to reply
  to and nothing configured, so nothing was sent, and that the remedy is to set
  `plugins.entries.refine.notify_target` to an explicit channel such as
  `'telegram:123456789'`, with `hermes send --list` naming the options.
- **An address was tried and failed:** say delivery to that address failed, to check it is
  reachable and spelled as `hermes send --list` reports it, and that a bare platform name only
  routes when that platform has a home channel.

Both keep the two facts that are already right and load-bearing: **applied edits are
unaffected**, and identical failures drop to debug (now: for the next N minutes — state the
real number, derived from `_FAILURE_REPEAT_SECONDS`, not hardcoded twice).

Replace the `"(none)"` string literal with a module constant (`_NO_TARGET = "(none)"`) and use
it at both the call site and the comparison. A sentinel compared by literal in two files is
one typo from silently taking the wrong branch.

**Test:** assert the no-address case does **not** mention home channels, and the failed-address
case does. This is what makes the branch real rather than cosmetic.

---

## F3, F4 — WITHDRAWN (documentation-only, not doing)

The operator declined all documentation work. The edit-budget wording in AGENTS.md
and the ContextVar claim in SPEC-notifications-v2.md are left as-is on purpose: the
`max_edits_per_day: 10` on the host is intentional (raised for testing), and the
code already captures the active chat on the turn thread regardless of what the
spec says. Neither affects operation.

## F3 (withdrawn) — AGENTS.md contradicts the live host on the edit budget

**File:** `AGENTS.md`

**The contradiction.** AGENTS.md lists as a non-negotiable invariant:

> **The daily edit budget stands** (3/day). It is the blast-radius limit.

The live host runs `max_edits_per_day: 10`. **The 10 is deliberate** — the operator raised it
to make testing possible. So the document is what is out of date, and right now it reads as if
a stated invariant had been quietly weakened threefold.

**Fix.** Correct AGENTS.md to describe the guardrail as it actually is: a configurable daily
cap (`max_edits_per_day`, **default 3**, raised to **10** on the reference host for testing),
and keep the point that matters — it exists as the blast-radius limit, and it must stay
enforced and fail closed. Do not change any code or config: the number is intentional.

Grep for other places that assert `3/day` (README, other docs) and fix them in the same commit
so the repo does not disagree with itself in a second place.

No test. State in the commit that this is a documentation correction with no behaviour change,
and that the 10 was confirmed intentional.

---

## F4 (withdrawn) — the notifications spec states a false fact about ContextVars

**File:** `docs/SPEC-notifications-v2.md`

**The defect.** The spec says that in practice a raw thread started inside the asyncio task
does see the session ContextVars. **Measured on the live host: it does not** — a raw thread
started inside a coroutine read `None`. `threading.Thread.start()` does not copy the context
in CPython.

This matters because that spec is the reference the next implementer reads. As written it
suggests the hook-side capture and pass-down is a belt-and-braces nicety, when it is the only
thing that works.

**Fix.** Replace the claim with the measured result and make the consequence explicit: the
active chat **must** be captured on the turn's own thread and passed into the worker by hand;
reading it from the worker returns nothing. Keep the rest of the section.

The code already does the right thing — this is the document catching up to it. No code change,
no test. Say so in the commit.

---

## F5 — release v0.12.0

Only after F1–F4 are pushed and CI is green on the final SHA.

`plugin.yaml` still says `0.11.0`, and the last tag `v0.11.0` is 68+ commits behind.

**Minor, not patch** — this release adds a user-facing feature, takes on a new coupling to a
host CLI module, renames a module, and closes credential leaks:

1. Bump `version:` in `plugin.yaml` to `0.12.0`. Grep for the version string elsewhere (README
   badge, docs) and update every occurrence in the same commit — a version that appears in two
   places and disagrees is worse than one that is only slightly stale.
2. Commit, push.
3. Tag `v0.12.0` on that commit and push the tag.
4. Create the GitHub release with `gh release create v0.12.0`.

Release notes: write them from the actual commit log between `v0.11.0..HEAD`, grouped by what a
reader needs to know, not by commit order. Cover at least:

- **User-facing:** notifications on install and on each applied edit, routed to the active chat
  with a configured fallback.
- **Security:** four credential shapes that leaked or were mangled (Stripe restricted keys and
  webhook secrets, RFC 4716 SSH2 key blocks, `Token`/`ApiKey` auth schemes, quoted Bearer);
  prompt notes could disable core tools; `VAR=value` prefixes bypassed block rules.
- **Correctness:** tool failures that read as success (MCP `isError`, self-declared
  `status: error`); a reasoning model's discarded draft could beat its final answer; Unicode
  line separators defeated two line-oriented defences.
- **Operational:** the installer patched a checkout the gateway did not run; the proposal
  timeout was re-derived from measured latency; `trace.py` no longer shadows the stdlib
  `trace` module.
- **Declared limits** (link the three `docs/FINDING-*.md` files rather than restating them):
  version numbers refused as legacy IPv4 hosts, the `count_today_applied` backward-seek being
  unsafe, and a bare `notify_target` being undeliverable.

Do not invent items that are not in the log, and do not claim a fix is verified live unless a
commit says it was.

---

## Order and stop condition

F1 → F2 → F5. (F3 and F4 are withdrawn — documentation-only, declined.)

F1 and F2 are already implemented and shipped in commit 61eec28; if you are reading
this fresh, only F5 (the release) remains.

Done when, each read rather than assumed:

- F1 and F2 committed with the fail-first output quoted for F1.
- Suite green after every commit, count stated.
- `py_compile` clean, `git diff --check` clean, nothing stray committed.
- CI green **4/4 on the final SHA**. CI cancels in-progress runs, so only the last one counts —
  confirm with `gh run list --limit 1 --json databaseId` then
  `gh run view <id> --json jobs`.
- `v0.12.0` tag pushed and the release published.

**Stop and report instead of improvising if:** the F1 rename leaves a call site you cannot
find, a test needs `notify()` to raise or block to pass, F3's grep turns up a place where
`3/day` is enforced in **code** rather than documented (that would mean the host config and the
code disagree, which is a different and larger finding), or the release would need a version
scheme change.
