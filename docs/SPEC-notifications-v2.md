# Spec — notifications v2 (junior handoff)

Exactly **two** user-facing messages, both minimal, both working for any user on any
platform without hand-configuration.

Baseline: `main` at `e5b1d81` or later — read HEAD yourself. Suite must be green before you
start; record the real count (it was 853 tests, OK, 6 skipped).

Everything below was measured on the live reference host (Hermes v2026.8.31,
`/home/ubuntu/releases/hermes-agent-v2026.8.31-clean`). Observed output is quoted so you can
confirm before changing anything.

---

## Non-negotiable rules (AGENTS.md governs)

1. **Python 3 standard library only.** No new dependencies, ever.
2. Run the suite from the repo root: `$env:PYTHONIOENCODING='utf-8'; python -m tests.run_tests`.
   Reading the output is the evidence. A command that ran is not evidence.
3. **One commit per item.** The message explains *why*. Author is already configured — commit
   normally, never touch git config. **Push after every commit.**
4. Never commit `__pycache__` or scratch files. Delete probes before committing.
5. **Fail-first is mandatory** for every behaviour change: write the test, run it against the
   parent, **read** the failure, quote it in the commit message. If a test passes on the parent,
   say so and label it hardening — do not fake a fail-first.
6. `git diff --check` clean and `python -m py_compile <file>` clean before each commit.
7. Cross-platform: `pathlib`, no `fcntl`, no shell-outs, no POSIX assumptions. The same file runs
   on a Linux server and a Windows desktop.
8. **A notification must never change a refine outcome.** Not by raising, not by blocking, not by
   delaying registration. This is the whole reason `notify.py` exists as an isolated module.
9. **Scrub stays at the choke point.** Anything leaving for the user goes through
   `sanitization.scrub_text`, even when the caller already scrubbed.

---

## Decisions already taken by the owner — implement, do not relitigate

- **Message 2 body is exactly one line:** `♾️ Refine Cycle — new lesson learned`. Nothing else.
  No `kind: name`, no undo hint, no journal id. Rationale: it must not distract. The rollback id
  remains discoverable in the journal and via `/refine-cycle audit`.
- **Plain text, no Markdown.** The old body used `**bold**`. Telegram's adapter sends
  `MARKDOWN_V2` with a `parse_mode=None` retry, so bold either renders or degrades to literal
  asterisks depending on platform. One line of plain text renders identically everywhere, which
  is the point of "works on all platforms".
- **Message 2 must work out of the box for every user**, not only where a home channel happens to
  be configured. This is N1 below and is the largest item.
- **Message 1 moves out of the installer into the plugin's first successful load.** Rationale in
  N3.

---

## N1 — make message 2 deliverable for every user on every platform

**The problem, measured.** `config.notify_target()` defaults to `"telegram"` — a bare platform
name. Hermes routes a bare name only when that platform has a home channel configured. On the
reference host `TELEGRAM_HOME_CHANNEL` was commented out in `~/.hermes/.env`, so every
notification failed:

```
No home channel set for telegram to determine where to send the message.
```

`cmd_send` exited 1. The current mitigation is a config key the operator must set by hand
(`notify_target: telegram:Taras`) — that is not "works for every user", and the default is also
wrong for anyone whose platform is not Telegram.

**The fix:** resolve a real target automatically when the operator has not chosen one.

Two host APIs make this possible. Both were verified in-process on the reference host:

```python
from gateway.channel_directory import load_directory
load_directory()
# {'updated_at': '2026-09-01T14:38:57.287175',
#  'platforms': {'telegram': [{'id': '6667956926', 'name': 'Taras',
#                              'type': 'dm', 'thread_id': None},
#                             {'id': '-1003790284798', 'name': 'Мем Фактура 2.0',
#                              'type': 'group', 'thread_id': None}]}}

from gateway.config import load_gateway_config
[getattr(p, "value", str(p)) for p in load_gateway_config().get_connected_platforms()]
# ['telegram', 'whatsapp']
```

Note `whatsapp` is connected but has **no** discovered channels — a platform being connected does
not mean it has somewhere to send.

### Implementation, in `notify.py`

Add `_resolve_default_target() -> Optional[str]`.

Algorithm, in this order:

1. **An explicitly configured target always wins.** If the operator set `notify_target` in
   `config.yaml`, use it verbatim and never auto-resolve. Detect "explicitly set" by reading the
   raw entry, not by comparing against the default string — otherwise an operator who
   deliberately sets `telegram` is indistinguishable from one who set nothing. Add
   `config.notify_target_configured() -> bool` (or an equivalent that returns `Optional[str]`
   with `None` for absent) next to `notify_target()`, reusing the existing accessor style in
   `config.py`. Do not invent a new config-reading mechanism.
2. Otherwise call `load_directory()` and keep only platforms that also appear in
   `get_connected_platforms()`. A channel on a disconnected platform cannot be delivered to.
3. Among the remaining channels prefer, in order:
   - `type == "dm"` — a personal notification belongs in a DM;
   - any other type only if no DM exists anywhere.
   **Never prefer a group over a DM.** "New lesson learned" in a shared group is noise for
   everyone else in it, and the trajectory it came from is private.
4. Iterate platforms in a **deterministic** order (sort the platform names) so the chosen target
   does not change between restarts for no reason.
5. Build the target as `f"{platform}:{channel_id}"` using the channel **`id`**, not its `name`.
   Names contain spaces and non-ASCII (`Мем Фактура 2.0`) and can be renamed by the user; ids are
   stable. Confirm with a real send that an id works as a target — `telegram:Taras` (a name) was
   verified to work, an id was **not**, so verify before relying on it. If ids turn out not to be
   accepted, use `name` and say so in the commit message.
6. If the directory is empty or unusable, fall back to the bare platform name of the first
   connected platform. That is exactly today's behaviour, so this is not a regression: it works
   where a home channel exists.
7. If there is nothing at all, return `None`. `notify()` must then **not** attempt a send, and
   must report once through the existing `_report_send_failure_once` path with a reason saying no
   target could be resolved.

### Failure isolation and caching

- `load_directory` and `load_gateway_config` are **another undocumented coupling to host
  internals**, exactly like `cmd_send`. Wrap every call: any exception means "cannot resolve",
  fall through to step 6, never raise. Import them lazily, inside the function, so a disabled
  `notify_enabled()` still costs nothing (there is an existing test asserting the disabled path
  imports nothing — keep it passing).
- Cache the resolved target for the process lifetime, but **invalidate it when a send fails**, so
  a channel that disappears is re-resolved instead of failing forever. Guard the cache with a
  `threading.Lock`; `notify()` is called from hook threads and the gateway runs several channels
  at once.

### Tests for N1

Both directions, and the negative cases are the load-bearing ones:

- An explicitly configured `notify_target` is used verbatim and auto-resolution never runs
  (patch `load_directory` and assert it was not called).
- With no configured target and a directory containing a DM and a group on the same platform, the
  **DM** is chosen.
- With only a group available, the group is chosen (a message is better than silence) — assert
  this is a deliberate second choice, not the first.
- A channel on a platform absent from `get_connected_platforms()` is never chosen.
- Two platforms both offering a DM resolve deterministically across repeated calls.
- `load_directory` raising falls back to the bare platform name and does not raise.
- An empty directory and no connected platforms returns `None`, sends nothing, and reports once.
- A failed send invalidates the cache, so the next `notify()` re-resolves.
- `notify()` still never raises and still returns within the join timeout on every new path.

---

## N2 — reduce message 2 to a single line

**Current**, verified live via the production call path (`core._notify_lesson`):

```
'♾️ **Refine Cycle** — new lesson learned\n\nmemory: refine-delivery-verification\n↩ undo: /refine-cycle rollback 582cdeaea333'
```

**Required:**

```
♾️ Refine Cycle — new lesson learned
```

One line. No trailing newline, no blank line, no markdown, nothing appended.

### Implementation, in `core.py`

`_notify_lesson` currently takes `kind`, `name` and `journal_id` and builds the label and the undo
hint from them. With the body reduced, none are used. Simplify the function to take no arguments
and update its single call site (the `outcome == "applied"` branch, near the ledger write).

Do **not** keep unused parameters "for later". Do **not** delete the surrounding comment
explaining that the call happens after the journal entry exists and only for `outcome=applied` —
that ordering is still the contract.

`core._command_display()` and `core._set_command_display_provider()` exist **only** to render the
command name inside this body. Check whether anything else uses them (grep both names). If nothing
does, remove them and the `core._set_command_display_provider(_command_display_name)` line in
`register()`. If something else does use them, leave them alone and say so. Removing a resolver
that is still needed elsewhere is worse than leaving a small unused helper.

### Tests for N2

- The delivered body equals the single required line **exactly** — assert equality, not
  `assertIn`. An `assertIn` cannot catch appended text, which is the thing being removed.
- The body contains no `**`, no `rollback`, no `undo`, no kind and no name. Assert the absence of
  a name you passed into the applied-edit path, so a future regression that re-adds it fails here.
- `test_applied_edit_notifies_once` currently asserts `assertIn("rollback", calls[0])`. That
  assertion is now wrong by decision, not by accident. Update it to the new contract and say so
  explicitly in the commit message. This is the one existing assertion you are permitted to
  change; do not weaken any other.
- Still exactly one notification per applied edit, and still zero for `dry_run`, `no_op`,
  `rejected`, `prepared`, `error`, `pending_approval`, `llm_error`. The existing subTest covering
  this must keep passing untouched.

---

## N3 — message 1 fires on the plugin's first successful load, not from the installer

**Why it moves.** The install message is currently sent by `notify_installed()` at the end of
`install.sh`. Measured, it cannot arrive on the documented install path:

- README's documented sequence is `hermes plugins install …` / `plugins enable` / `gateway
  restart`. That path **never executes `install.sh`**, so the message is never sent.
- `install.py` — the script that actually copies the plugin into `<HERMES_HOME>/plugins/refine` —
  contains **no** notification at all (grep for `notify` and `hermes send`: zero matches).
- Even on a manual `./install.sh`, the first line of `notify_installed()` is
  `command -v hermes >/dev/null 2>&1 || return 0`. On the reference host `hermes` is **not** on
  PATH (it lives inside the release venv), so the function returns silently.
- And past that, it sends to the same bare `telegram` target that N1 exists to fix.

Four independent silent-failure gates. The plugin itself is the only component that knows it
actually loaded.

### Implementation, in `__init__.py`

Add a first-load notification fired at the **end** of `register()`, after registration has
succeeded — never before, so a message can never claim a plugin that failed to register.

**Body:**

```
🔌♾️ Refine Cycle — installed and active
```

One line, plain text, matching message 2's style.

The old text said `Please restart the gateway: sudo systemctl restart hermes-gateway`. That
instruction is now **false** and must not be carried over: the message is sent from inside
`register()`, which only runs because the gateway already loaded the plugin. Telling the user to
restart something that is already running is worse than saying nothing.

**Fire exactly once ever, not once per gateway restart.** `register()` runs on every gateway
start, so an unlatched message would arrive on every restart.

- Latch with a marker file in `config.journal_dir()` — the plugin's own data directory, already
  gitignored and already the home of runtime state. Do not put it in the plugin source directory.
- Create the marker with `open(path, "x")` (atomic create, cross-platform). The gateway can run
  several processes; atomic create means exactly one wins the race and the losers see
  `FileExistsError` and stay silent.
- **Write the marker BEFORE sending.** If the marker cannot be written, do **not** send. A missed
  greeting is a cosmetic loss; a greeting on every restart is a recurring annoyance the user
  cannot switch off. Silence beats spam.
- Respect `config.notify_enabled()`. When it is False, do not send and do not create the marker —
  an operator who enables notifications later should still get the greeting.

**`register()` must not block.** `notify.notify()` already runs `cmd_send` on a worker thread with
a join timeout, but that join still costs up to the timeout. Fire the whole greeting from a
`threading.Thread(daemon=True)` so registration returns immediately, and wrap it so nothing can
escape into the host's plugin loader. A plugin that raises during `register()` may not register at
all — the exact failure this must not cause.

### Remove the installer's copy

Delete `notify_installed()` and its call from `install.sh`. Keeping both would send two greetings
to anyone who does run `install.sh`, and the owner asked for exactly two messages total. Say in
the commit message that this is a deliberate move, not a deletion of a working feature, and note
that `REFINE_NOTIFY_TARGET` becomes unused — remove its mention from any docs that describe it.

### Tests for N3

- A first `register()` sends exactly one greeting whose body equals the required line exactly.
- A second `register()` in the same or a new process sends **nothing** (marker present).
- With `notify_enabled()` False: no send **and** no marker created, so a later enable still
  greets.
- A marker directory that cannot be written: no send, no raise.
- `notify` raising inside the greeting does not propagate out of `register()`, and registration
  still completes — assert the command and hooks are still registered.
- `register()` returns without waiting for the send: patch `notify` to block and assert
  `register()` completes promptly.
- Two threads calling `register()` concurrently produce exactly one greeting. Per AGENTS.md a
  concurrency test must actually start two threads.

---

## Order

N1 → N2 → N3.

N1 first: it is what makes any message arrive at all, and N2/N3 are pointless until it works. N2
before N3 so the one-line body and its exact-equality test exist before N3 reuses the same style.

---

## Verification standard

Green tests are a secondary signal. Every serious defect in this project so far was invisible on
synthetic input and obvious on real data — including the one this spec fixes, which was green
through an entire audit.

- After N1, **verify auto-resolution against the real host**, not only fixtures: no configured
  `notify_target`, and confirm a message arrives. Read the resolved target and say what it was.
- After N2, verify the delivered body on the real host is byte-for-byte the single line.
- After N3, verify the marker file appears once and a second `register()` is silent.
- State plainly what you verified and what you could not. If a path could not be exercised, say so.

## Stop condition

- Every item committed separately and pushed, author correct.
- Suite green after each commit, count read and stated.
- `py_compile` clean, `git diff --check` clean, no `__pycache__`, no probe files.
- CI green **4/4 on your final SHA**. CI cancels in-progress runs, so only the last SHA's run
  matters — confirm with `gh run list` then `gh run view <id> --json jobs`.
- A final note stating, per item: what was reproduced, the fail-first output observed, and whether
  it was a behaviour fix or hardening.

**Stop and report instead of improvising if:** channel ids turn out not to be valid send targets,
`load_directory` does not exist on the host you test against, removing `_command_display` breaks
something you did not expect, or the N3 marker cannot be made race-safe without a lock file.

## Out of scope

Do not touch: the base system prompt, `state.db` access mode, the daily edit budget, the
signal gate, fingerprinting, or CI. Do not add abstractions, folders or helpers that do not remove
existing complexity. If a change turns out to require re-architecture, say so with the scope and
risks before doing it.
