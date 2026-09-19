# Audits

External audits of Refine Cycle and what came of them. Every finding was checked against the code before anything changed. A finding counts as real only if a test fails on the old code and passes on the fix, and the test stays in `tests/run_tests.py`.

## Round 1 — 2026-09-19, v1.3.15

Auditor: Apodex (Deep Solve), six separate runs. Verification and fixes: Claude, in commit [2291d8a](https://github.com/Bergschloss/Refine-Cycle-for-Hermes-Agent/commit/2291d8a).

| Angle | Claimed | Real | Fixed |
|---|---|---|---|
| Self-update | 2 | 1 | 1 |
| Crash and concurrency | 2 (+1 unproven) | 1 | 1 |
| Prompt injection and secrets | 4 | 1 | 1 |
| Hermes compatibility | 2 | 0 | — |
| Desktop card and status bar | 3 | 3 | 3 |
| Tests that pass while broken | 2 | 1 | 1 |
| **Total** | **15** | **7** | **7** |

### Fixed

- **A torn journal record stopped refine for good.** A crash while appending to the journal left a partial last line. The whole journal then read as unreadable, refine refused to run, and nothing ended that state. Now the partial record is not treated as part of the journal: it is skipped on read and moved to `refine_journal.jsonl.torn` on the next append. Any other bad line still stops the journal, as before. The auditor rated this one unproven. It turned out to be the most serious finding of the round.
- **Three secret formats got past the scrubber:** a private key written in lowercase, a key cut off before its `END` line (common in logs), and `user:password@host` without a scheme.
- **A release whose Python does not parse installed anyway.** The release's own installer only imports `core`, so a broken `__init__.py` got through. Every module is now compiled before install.
- **Desktop card.** No card under a reply that ends inside an unclosed code block, where it would render as code; it moves to the next reply instead. No card in an app too old to render it, where the status bar still has the buttons. Disabling the plugin now cancels a backend restart it had already scheduled.
- **One test passed either way.** `update_check: false` was supposed to skip the release lookup, but the test's failing mock was swallowed by the code under test. The test now proves no lookup happens.

### Refuted

- **A prompt note that says "use X rather than Y" is a durable override.** Correct, and that is the feature. Notes still cannot name URLs, hosts, paths or shell syntax.
- **A skill may contain commands and URLs.** A skill documents how to do a job, and commands are what it is made of.
- **A session note can be planted in another session.** The auditor called the validator directly. On the real path, normalization binds the note to the caller's session before validation runs.
- **An edit is lost when the process dies before applying it.** That is the recovery model: the edit never landed, and the failure is offered again later. The proposed fix would have closed a record that a second, still-running process owns.
- **Rollback can remove the user's identical entry.** It needs a byte-identical user entry written in the same instant, and even then one of the two identical copies remains.
- **Hermes symbols have moved.** Accurate, but already handled: the installer applies the patch that matches the host.

One finding belongs to Hermes itself and is being reported there.

## How the checks work

1. **Audit.** The auditor gets one angle per run and must give a code trace, a breaking input, a failing test and a fix for every finding.
2. **First check.** A second agent runs each test on the old code, applies the fix on a branch, and runs the full suite: 1,320 tests on Windows and Linux, plus the desktop probe.
3. **Staging, from round 2.** Fixes that pass run on a separate Hermes install with real hooks and a real gateway, next to a live one that is never touched.
4. **Final review.** Claude reviews every fix that touches the journal, locks, rollback, security or the installer before it merges.
