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
- **Two private-key formats got past the scrubber:** a key written in lowercase, and a key cut off before its `END` line (common in logs). A rule for `user:password@host` without a scheme was added too, then removed: it also hid package names and addresses that tell two errors apart. **Superseded:** the credential filter was removed entirely after this round. It hid nothing from the model — Hermes gives the same conversation to the same model itself — while breaking JSON tool results and merging distinct errors into one fingerprint, so these fixes no longer exist to be relied on.
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

## Round 2, 2026-09-19

Auditor: Apodex, six angles it chose itself. First check: Antigravity, in a clone and then on a separate staging Hermes next to a live one. Final review and fixes: Claude.

| Angle | Claimed | Real | Fixed |
|---|---|---|---|
| Installer on unusual checkouts | 3 | 3 | 3 |
| Error fingerprints | 5 | 5 | 5 |
| Daily limits and clocks | 3 | 1 | 1 |
| "Did the lesson help" grader | 4 | 0 | none |
| Rollback after a hand edit | 0 | 0 | none |
| Data growth | 3 | 0 | none |
| Found on the staging Hermes | 2 | 2 | 2 |

### Fixed

- **Memory rollback failed on every real Hermes.** Current Hermes writes memory through `_write_file`. It has no `save_to_disk`, so the rollback raised an error. The fake host in the tests still had the old method, so the suite passed anyway. The staging run found this. Rollback now writes through whichever writer the host has.
- **`/refine_fix` could not repair a half-patched Hermes.** When an update or a `git checkout` restores some patched files but not others, no patch reverses cleanly, and the installer refused to continue. It now takes a snapshot, returns the files to the checkout's own versions, and patches again, the same way it handles an outdated patch.
- **Installer.** A systemd `ExecStart=-…` prefix hid the Hermes checkout. Rollback failed when a folder had been removed. A rollback whose patch file no longer ships could restore the wrong set of files; it now refuses instead.
- **Error fingerprints.** "exited with code 1" and "exited with code 137" merged into one fingerprint, and so did two different ports on `[::1]`. Other cases split one error into two: a quote cut off at the end, a Python exception group, and `main.py:42` read as a host and port.
- **A timestamp years in the future** blocked a repeat edit and stretched the cooldown by years. Such timestamps are now ignored.
- **Outcome of a failure.** The window that follows a failure grew from 6 messages to 24. A repair the agent actually made now outranks the word "stop" in what it wrote.

### Rejected

- **Grader changes.** `lesson_effect_checker.py` is the frozen grader of the pre-registered experiment, and its SHA-256 is in the report. Changing it would change how finished results are scored.
- **Reading the journal backwards.** It read raw lines. In the journal one edit has several records, so this reported rolled-back edits as applied.
- **A one-year limit on backups of applied edits.** It would silently end the promise that every applied edit can be undone.
- **A cap on the audit window, and a ledger change.** Each broke tests that guard deliberate behaviour.
- **Daily limits keyed to the UTC day.** Correct, and intended.

## How the checks work

1. **Audit.** The auditor gets one angle per run and must give a code trace, a breaking input, a failing test and a fix for every finding.
2. **First check.** A second agent runs each test on the old code, applies the fix on a branch, and runs the full suite on Windows and Linux, plus the desktop probe.
3. **Staging, from round 2.** Fixes that pass run on a separate Hermes install with real hooks and a real gateway, next to a live one that is never touched.
4. **Final review.** Claude reviews every fix that touches the journal, locks, rollback, security or the installer before it merges.
