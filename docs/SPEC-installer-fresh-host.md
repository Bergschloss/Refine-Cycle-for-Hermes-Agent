# Spec — the installer cannot install on the version we ship for

Handoff spec for a junior model. Three items, all bug fixes. **No new capability.**

Baseline: `main` at `8d4747b` or later — read HEAD yourself. Suite is **881 OK (skipped=6)** at
`8d4747b`; confirm it before you start and do not inherit the number.

Context: `install.py` is the cross-platform installer and the final acceptance gate for shipping.
It has no tests at all, which is why the defects below shipped. One of them — the installer
copying a plugin that could not be imported — is already fixed in `8d4747b`, with the first two
tests the file has ever had. The rest is this spec.

---

## Measured facts. Read, not reasoned.

**The reference host runs v2026.8.31.** `git -C $HERMES rev-parse --short=8 HEAD` → `29112bef`,
`git describe --tags` → `v2026.8.31`.

**`install.py` refuses to install on it.** A probe built two synthetic checkouts and called the
real `classify_host()`:

```
EXPECTED_BASE_PREFIX = 'df4b6514'
PATCH_FILE           = invocation-route-v2026.8.16.patch
CLEAN (new user) head=1d53d48243  ->  incompatible
                 detail: unsupported base 1d53d48243; this patch targets stock v2026.8.16
already patched  head=28a0e8a083  ->  patched
                 detail: all 8 files carry the marker at 28a0e8a083
```

`do_install()` turns `incompatible` into `fail()`. So **a fresh install on 8.31 is impossible**,
and `--patch-only` likewise.

**Why nobody noticed.** On an *already patched* host the marker check short-circuits the version
check and the state comes back `patched`. The reference host was patched by hand via `install.sh`,
so `install.py --status` reports healthy there:

```
State           : patched — all 8 files carry the marker at 29112bef09
```

The defect only bites the case nobody had run yet: a clean host. That is exactly the acceptance
gate.

**Both patches are bundled and both fit the same file set.** `assets/` holds
`invocation-route-v2026.8.16.patch` and `invocation-route-v2026.8.31.patch`; each touches the same
9 paths that `PATCH_FILES` + `PATCH_TEST_FILE` list, so the hardcoded list is not currently
drifting. `PATCH_FILE` points at the 8.16 one.

**The 8.31 patch does fit 8.31.** On the patched reference host,
`git apply --check --reverse assets/invocation-route-v2026.8.31.patch` → rc 0. A patch that
reverses cleanly out of the patched tree is the patch that applies cleanly to the unpatched one.

**`install.sh` already solved this and wrote down why.** From its own comments: which bundled
patch fits *this* host is decided by trying them, not by comparing version strings — Hermes moved
72 commits across these files between v2026.8.16 and v2026.8.31, yet the 8.16 patch still lands 39
of its 40 hunks on 8.31, so a version test refuses hosts the patch fits and accepts hosts it does
not. `install.py` was simply left behind.

**The 8.31 patch is not a git patch.** It was produced with `diff -ruN`, so it has no
`diff --git` line and its headers read `+++ b/path<TAB>timestamp`. `install.sh` needed a special
parse for that (`6345d0f`). `git apply` itself handles both, and `install.py` reads no file list
out of the patch, so this only matters if you start parsing one.

**`.git` on the reference checkout is a FILE, not a directory** — it is a linked worktree
(`gitdir: /home/ubuntu/hermes-agent/.git/worktrees/...`). Any check written as
`(src / ".git").is_dir()` is wrong. `git rev-parse` is the correct probe and is what the code
already uses.

**18 tests never execute.** `tests/test_usefulness.py` (10), `tests/test_block_rule_fallback.py`
(5), `tests/test_proposer_status.py` (3) are not imported by `tests/run_tests.py`, and CI runs
only `python tests/run_tests.py`. Not one of their 18 method names appears in `run_tests.py`, so
this is lost coverage, not duplication. Among them is
`test_delete_proposal_never_eligible` — invariant 1 in AGENTS.md is guarded by a test that has
never run.

---

## Non-negotiable rules (AGENTS.md governs)

1. **Python 3 standard library only.** No new dependencies, no new files except where stated.
2. Suite from the repo root: `$env:PYTHONIOENCODING='utf-8'; python -m tests.run_tests`.
   Reading the output is the evidence.
3. **One commit per item**, message explains *why*. **Push after every commit.**
4. **Fail-first is mandatory:** write the test, run it against the parent, **read** the failure,
   quote it in the commit message.
5. `git diff --check` clean and `python -m py_compile install.py` clean before each commit.
6. **`py_compile` is not enough for this file.** The defect I created while exploring proves it:
   removing the `PATCH_FILE` constant left four live references to an undefined name
   (`apply_patch_atomic` ×3, the `partial` branch of `do_install` ×1). It compiled fine and would
   have raised `NameError` during a real install. Grep for every reference to any name you remove.
7. **New tests go in `tests/run_tests.py`.** It is the only file CI executes. A test in a sibling
   file does not run — see item I3.
8. **DO NOT TOUCH `notify.py` or its tests.** A review of that file is in flight.
9. Another model is working on `core.py`, `journal.py`, `llm.py`, `patterns.py` under
   `docs/SPEC-memory-budget-and-honest-refusal.md`. **Stay out of those four files.** This spec
   needs `install.py` and `tests/run_tests.py` only. Pull before you start and before each push.

### Live host

```
ssh oracle-imma
plugin checkout : ~/.hermes/plugins/refine
hermes source   : /home/ubuntu/releases/hermes-agent-v2026.8.31-clean   (PATCHED, live)
interpreter     : $HERMES/.venv/bin/python
suite on host   : cd ~/.hermes/plugins/refine && PYTHONIOENCODING=utf-8 $HERMES/.venv/bin/python -m tests.run_tests
```

- **Never patch, unpatch, or `git checkout` inside the live Hermes checkout.** The gateway runs
  from it.
- Never edit `~/.hermes/config.yaml`; provider and model selection are settled.
- Long commands must outlive SSH — `nohup`/`setsid` do not survive here:
  `sudo -n systemd-run --unit=NAME --collect --uid=ubuntu --setenv=HOME=/home/ubuntu --working-directory=$HERMES --property=TimeoutStartSec=900 -- <cmd>`
- Never restart the gateway from inside a gateway turn; same `systemd-run` pattern.

---

## I1 — find the patch that fits by trying it, not by pinning a commit

**Files:** `install.py`, `tests/run_tests.py`

Replace the version gate with an applicability test. This design was written, tested and then
reverted (the user wanted the plan first, and the half-applied edit was the `NameError` above), so
the shape below is validated by tests that ran, not sketched.

**Add:**

```python
PATCH_DIR  = PLUGIN_DIR / "assets"
PATCH_GLOB = "invocation-route-*.patch"

def _patch_sort_key(path: Path) -> tuple[int, ...]:
    """Numeric ordering. A lexical sort puts v2026.10.1 before v2026.9.2."""
    return tuple(int(n) for n in re.findall(r"\d+", path.name))

def patch_candidates() -> list[Path]:            # newest base first
    return sorted(PATCH_DIR.glob(PATCH_GLOB), key=_patch_sort_key, reverse=True)

def select_patch(src) -> Path | None:            # git apply --check
def select_reverse_patch(src) -> Path | None:    # git apply -R --check
```

`re` is not currently imported in `install.py`; add it.

**Remove `EXPECTED_BASE_PREFIX` as a gate.** Keep the base identity as a comment or an
informational constant only. Both places it is used today go:

- the early `not head.startswith(...) and not has_marker` refusal — delete it;
- the `head.startswith(...)` condition guarding the dirty check — the dirty check must run
  unconditionally for an unpatched base.

**Order inside `classify_host` matters.** Check the tree is clean *before* checking
applicability: a user's uncommitted edit to a patch target can itself make `git apply --check`
fail, and reporting that as "no patch fits this host" sends them chasing a version problem they do
not have. So: `not-a-checkout` → `patched` → `partial` → marker-outside-expected-files →
**dirty** → `select_patch` → refuse.

**The refusal must name what was tried**, the way `install.sh` does:
`no bundled route patch applies to base <head>; tried: <names>. The patch needs rebasing onto this
host's version.` "Cannot patch this core" is a fine outcome; "refused without trying" is not.

**Thread the chosen patch through — this is where the `NameError` lives.** `PATCH_FILE` currently
appears at four live call sites. `apply_patch_atomic(src, meta_dir)` must take the patch as an
argument and use it for `--check`, for the apply, and for its own failure reversal. The `partial`
branch of `do_install` does *not* know which patch was half-applied, so it must use
`select_reverse_patch()`. Record the chosen patch in the install metadata
(`meta["host"]["patch"] = chosen.name`) — the rollback path restores from the backup zip and does
not need it, but a metadata file that cannot say which patch was applied makes the next
diagnosis guesswork.

**Do NOT add `install.sh`'s tolerant apply ladder** (`-3`, `-3 -C1`, `-3 -C0`) in this item.
Selection alone makes 8.31 installable, and decreasing-context three-way merges are a separate
behaviour with their own verification burden. If you think it is needed, say so and stop.

**The module docstring becomes false** the moment this lands: it says `Supported Hermes base:
stock v2026.8.16 (commit df4b65147d...)`. Correct that sentence — it is the file's own contract,
not documentation work.

### Tests for I1 (all six ran; the first is the fail-first)

Build the fixtures from a throwaway `git init` checkout containing
`install.ALL_PATCH_CONTENT` as stub files, and generate a patch that is *guaranteed* to apply by
diffing that checkout against itself (append the marker, `git diff`, restore). Write the patch
file **outside** the checkout, or it shows up as untracked and breaks the dirty test.

1. **`test_the_version_prefix_is_not_a_gate`** — a synthetic host is still refused (no bundled
   patch fits it), but the *reason* must no longer be its commit id. Assert `"unsupported base"`
   is absent from the detail and the refusal says what was tried. This is the one that pins the
   defect without depending on the new API. Read failing against the parent:

   ```
   AssertionError: 'unsupported base' unexpectedly found in
   'unsupported base e2783dd4ca; this patch targets stock v2026.8.16'
   : host refused on its commit id rather than on whether a patch applies
   ```

2. `test_a_base_the_patch_fits_is_installable` — with `patch_candidates` patched to a patch that
   fits, a clean non-8.16 host classifies `stock`, and the detail names the chosen patch.
3. `test_the_more_specific_patch_wins_when_both_apply` — when two fit, `select_patch` returns the
   newer base.
4. `test_bundled_patches_are_ordered_newest_first` — the real bundled patches order 8.31 before
   8.16, **and** `_patch_sort_key` puts `v2026.10.1` after `v2026.9.2`. A lexical sort passes the
   first assertion and fails the second; that is the point of having both.
5. `test_user_modified_targets_are_dirty_not_incompatible` — a modified `hermes_cli/plugins.py`
   yields `dirty`, not a refusal, even though the patch no longer checks clean.
6. A both-directions counterpart for the refusal: an empty `assets/` (patch `patch_candidates` to
   `[]`) refuses with `none bundled` rather than raising.

### Acceptance for I1 — a real clean 8.31 tree

Synthetic fixtures prove the logic; they do not prove the bundled 8.31 patch applies to real 8.31
sources. Get a genuine clean tree **without touching the live checkout**:

```
git -C /home/ubuntu/hermes-agent worktree add --detach /tmp/h831-probe v2026.8.31
cd ~/.hermes/plugins/refine && python3 install.py --status --hermes-src /tmp/h831-probe
```

Expect `stock — clean base <head>; invocation-route-v2026.8.31.patch applies`. Then run a real
`--patch-only` install against `/tmp/h831-probe`, confirm it applies and compiles, and
`--rollback` it. Clean up: `git -C /home/ubuntu/hermes-agent worktree remove /tmp/h831-probe`.
This adds and removes one worktree entry in the Hermes repo and never touches the running tree —
but confirm `git worktree list` is back to its previous contents when you are done, and report
what it showed before and after.

**Quote the `--status` output for the clean tree in the commit message.** Without it this item is
unverified, because every other test is synthetic.

---

## I2 — `--plugin-only` is accepted and silently ignored

**Files:** `install.py`, `tests/run_tests.py`

`main()` branches on `--status` and `--rollback` and otherwise calls `do_install(args)`.
`do_install` reads `args.patch_only` but **never reads `args.plugin_only`**. So on a `stock` host,
`python install.py --plugin-only` patches the Hermes core — the one thing the flag exists to
prevent. On an already-patched host it happens to behave, which is why it looks fine.

This is the exact failure `install.sh` fixed in itself; its header records it: *`--patch-only` was
documented and never implemented; it was silently ignored, which read as if it had been honoured.*

- Honour it: with `--plugin-only`, skip `classify_host`-driven patching entirely, install the
  plugin files, run the import verification from `8d4747b`, write metadata, and **do not** run the
  host capability check (it asserts host markers that a `--plugin-only` user may not have — a
  false failure).
- Reject the contradiction: `--patch-only --plugin-only` together is not a state, so `fail()`
  with a message naming both rather than silently letting one win.
- Say what was skipped. A `--plugin-only` run on an unpatched host must print that the host
  capability is absent and that `refine_run` will return `llm_invocation_unavailable` until
  `--patch-only` is run. Silence here is how the original bug hid.

**Fail-first:** on a synthetic `stock` host, assert `--plugin-only` leaves the patch targets
byte-identical. Against the parent it patches them, so the assertion fails. Both directions:
`--plugin-only` on a patched host still installs the plugin and still does not touch the host.

---

## I3 — 18 tests that never run

**Files:** `tests/run_tests.py` (and possibly delete three files)

`tests/test_usefulness.py`, `tests/test_block_rule_fallback.py` and `tests/test_proposer_status.py`
hold 18 tests between them. `run_tests.py` does not import them and CI runs only `run_tests.py`,
so they have never executed. None of their names exists in `run_tests.py`; this is lost coverage.

**Do the safe half first, and report before doing the rest.** Import the three modules into
`run_tests.py` so their `TestCase` classes are discovered, then run the suite and **read what
happens.** Tests that have not run in a long time usually do not pass. Expect breakage.

- If they pass: keep them imported, note the new suite count, and consider whether the three files
  should be merged into `run_tests.py` or left as modules that `run_tests.py` imports. Either is
  fine; state which and why. **Do not raise the CI floor** in the same commit — the floor guards
  against tests vanishing, and moving it while adding tests makes both changes unreviewable.
- If they fail: **do not fix them in this item and do not delete them.** Report each failure with
  its message and stop. A failing test that has never run is either a real regression that shipped
  or a stale test, and telling those apart is its own task. Deleting them silently would remove
  coverage of `test_delete_proposal_never_eligible`, which guards AGENTS.md invariant 1.

`SuiteDiscoveryContractTests` already asserts a floor on discovered classes and re-imports the
suite module; check your import does not make that re-import recursive before you trust a green
run.

---

## Order and stop condition

I1 → I2 → I3.

Done when, each read rather than assumed:

- Three commits (I3 may be a commit plus a report), pushed, fail-first output quoted for each.
- Suite green after every commit, count stated. Baseline 881 at `8d4747b`.
- I1's clean-worktree `--status` output quoted, and `git worktree list` confirmed restored.
- `py_compile` clean, and every name you removed grepped for live references.
- CI green **4/4 on the final SHA** — CI cancels in-progress runs, so confirm the last one:
  `gh run list --limit 1 --json databaseId` then `gh run view <id> --json jobs`.
- A closing note, one line each: can a new user install on 8.31 now; does `--plugin-only` still
  touch the host; how many tests run now versus 881; and what remains unverified.

**Stop and report instead of improvising if:** the bundled 8.31 patch does not apply to a real
clean 8.31 worktree; `select_patch` picks a patch that then fails to apply for real; `--plugin-only`
cannot be honoured without restructuring `do_install`; the 18 revived tests fail; or any item would
need `notify.py`, `core.py`, `journal.py`, `llm.py` or `patterns.py`.

## Out of scope

- The tolerant apply ladder from `install.sh` (deliberate, see I1).
- Merging `install.sh` and `install.py` into one implementation. They are two implementations of
  overlapping work and that is worth fixing, but it is a redesign, not a bug fix.
- macOS. Entirely unverified for this installer; do not claim otherwise.
- Raising the CI floor, or extending CI in any way.
- The README. All documentation work is declined by the owner; the only prose to touch is the
  `install.py` module docstring sentence that this change makes factually false.
