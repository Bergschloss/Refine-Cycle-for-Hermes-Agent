# Spec — send the "lesson learned" notification to the active chat

Junior handoff. **One goal:** when refine applies a self-edit, the user gets one line in the chat
they are actually talking in.

Baseline: `main` at `abb2940` or later — read HEAD yourself. Suite green before you start; it was
**853 tests, OK, 6 skipped**. Record your own number.

Everything factual below was verified in-process on the live host (Hermes v2026.8.31). Observed
output is quoted so you can confirm before trusting it. Where something was *not* verified, it says
so.

---

## Out of scope — do not build these

- **The install/greeting message.** The owner explicitly deprioritised it ("похуй, як буде
  надсилатися перше повідомлення"). Leave `install.sh`'s `notify_installed()` exactly as it is. Do
  not add a plugin-side greeting, do not add a latch/marker file, do not touch `register()` except
  where §3 says.
- **Channel-directory scanning** (`gateway.channel_directory.load_directory`). An earlier draft
  used it to guess a DM. Superseded: it guesses where the user is instead of knowing. Do not
  implement it.
- Anything in AGENTS.md's "Scope discipline": no new abstractions, no reformatting, no CI changes,
  no new dependencies (stdlib only).

---

## Non-negotiable rules (AGENTS.md governs)

1. **Python 3 stdlib only.** No new dependencies.
2. Suite from the repo root: `$env:PYTHONIOENCODING='utf-8'; python -m tests.run_tests`.
   **Reading the output is the evidence.** A command that ran is not evidence.
3. **One commit per numbered section.** The message explains *why*. Author is already configured —
   commit normally, never touch git config. **Push after every commit:**
   `git -C G:/Kiro/Refine-Cycle push origin main`
4. **Fail-first is mandatory.** Write the test, run it against the parent, **read** the failure,
   quote it in the commit message. If a test passes on the parent, say so and call it hardening.
5. `git diff --check` clean and `python -m py_compile <file>` clean before each commit. No
   `__pycache__`, no probe files.
6. Cross-platform: `pathlib`, no `fcntl`. The same file runs on this Linux server and a Windows
   desktop. CI runs both.
7. **A notification must never change a refine outcome** — not by raising, not by blocking. That is
   why `notify.py` is an isolated module.
8. **Scrub stays at the choke point.** `notify()` already runs `scrub_text`; do not remove it and do
   not add a second one at a call site.

---

## The one rule that decides whether this works

The active chat lives in a **ContextVar the gateway sets per asyncio task**. Refine sends its
notification from a **worker thread**, started from a hook. The host confirmed explicitly:

> гарантованого наслідування ContextVar у raw `threading.Thread` як задокументований контракт —
> немає, хоч практично стартує всередині task'у воно й спрацьовує.

So:

> **Capture `platform` / `chat_id` / `thread_id` inside the hook callback. Pass the triple down by
> value. NEVER call `get_session_env` from a worker thread.**

This is exactly the pattern the plugin already uses for the LLM facade (`_session_llm()` is
resolved in the callback and passed into the thread, see the comment at `_on_post_llm_call`). You
are adding a second passenger to an existing, working convention. Follow it; do not invent a new
one.

If you get this wrong the tests may still pass and the feature will still *appear* to work on this
host, because inheritance happens to work today. It will break silently later. Treat the
"never in a worker" test in §5 as the point of the whole task.

---

## Host API, verified

```python
from gateway.session_context import get_session_env, session_is_messaging_surface
```

- `get_session_env(name, default="")` — reads the ContextVar, **falls back to `os.environ`** when
  the var was never set in this context. That fallback is what makes the probe recipe in §6 work.
- `session_is_messaging_surface() -> bool`. Verified `NON_MESSAGING_SESSION_SURFACES`:
  ```
  frozenset({'', 'codex', 'local', 'cli', 'webhook', 'api_server', 'desktop',
             'msgraph_webhook', 'tui', 'gateway', 'tool', 'kanban'})
  ```
- Verified `_VAR_MAP` contains exactly these names (do not guess spellings):
  `HERMES_SESSION_PLATFORM`, `HERMES_SESSION_CHAT_ID`, `HERMES_SESSION_THREAD_ID`,
  `HERMES_SESSION_CHAT_TYPE`, `HERMES_SESSION_CHAT_NAME`, `HERMES_SESSION_SOURCE`,
  `HERMES_SESSION_KEY`, `HERMES_SESSION_ID`.
- **Target format.** From `tools.send_message_tool._TELEGRAM_TOPIC_TARGET_RE` =
  `^\s*(-?\d+)(?::(\d+))?\s*$`. Verified matches: `6667956926` ✓, `-1003790284798` ✓,
  `-1001234567890:25` ✓, `Taras` ✗. So a **numeric chat id is a valid target with no directory
  lookup**, and `platform:chat_id:thread_id` addresses a forum topic.
- `load_gateway_config().get_home_channel(platform)` exists, takes a `Platform` **enum** (not a
  string), and returned `None` for both `telegram` and `whatsapp` on this host. It therefore earns
  no tier of its own — `send_message_tool` already falls back to it internally for a bare platform
  name.

### Measured: why CLI sessions still need a configured fallback

```
sessions total     : 187
with a known chat  : 13  (7.0%)
no chat (CLI/local): 174
   dm 12 | group 1 | (none) 174
```

The host's advice was "no chat → just skip". **Do not do only that.** On this owner's host that
would silence 93% of their work. Keep the configured `notify_target` as tier 2.

---

## Resolution order (implement exactly this)

1. **Active chat** — captured triple with non-empty `platform` and `chat_id` →
   `platform:chat_id`, plus `:thread_id` when present.
2. **`config.notify_target_configured()`** — the operator's explicit choice, for runs with no chat.
3. **Neither** → send nothing, report once via the existing `_report_send_failure_once`, return
   False. Do **not** fall back to a bare platform name: that is the silent-forever failure
   documented in `docs/FINDING-notify-bare-target-undeliverable.md`.

`config.notify_target_configured()` **already exists on main** (added in `9259fdd`) and returns
`Optional[str]` — `None` when the operator set nothing. Use it; do not add another accessor.

Note `config.notify_target()` (the old one, defaulting to `"telegram"`) becomes unused by the
notification path. Grep it. If nothing else uses it, remove it in §4 and say so; if something does,
leave it.

### A group chat is acceptable

The owner was asked and answered: active chat, whatever it is. This is safe **because** §2 strips
the body to one line with no kind, no name, no journal id and no evidence — zero trajectory
content. An operator who wants it elsewhere sets `notify_target`, which tier 2 honours.

---

## §1 — `notify.py`: resolve the target from a chat

Add near the top (after the imports; `Optional`/`Tuple` need adding to the `typing` import):

```python
def target_for_chat(chat: Optional[Tuple[str, str, str]]) -> Optional[str]:
```

- `chat` is `(platform, chat_id, thread_id)` or `None`.
- Returns `platform:chat_id[:thread_id]`, else `config.notify_target_configured()`, else `None`.
- Pure and side-effect free. **It must not import `gateway.*`** — capture happens in
  `__init__.py`, and keeping this function pure is what makes it testable without a host.

Change the signature to `notify(text: str, chat: Optional[Tuple[str, str, str]] = None) -> bool`.
Inside, after the existing `notify_enabled()` early return, replace
`target = config.notify_target()` with `target = target_for_chat(chat)` and add:

```python
        if not target:
            _report_send_failure_once(
                "(none)", "no active chat and no notify_target configured"
            )
            return False
```

Keep the existing scrub, the worker thread, the join timeout, and every existing early return
untouched. The default `chat=None` keeps every current caller working.

**Docstring must state** why `chat` is a parameter and not read here: the ContextVar rule above.

### Tests for §1

Pure-function tests, no host needed:

- `(("telegram", "6667956926", ""))` → `"telegram:6667956926"`.
- `(("telegram", "-1001234567890", "25"))` → `"telegram:-1001234567890:25"`.
- A chat wins over a configured `notify_target` (patch the config accessor, assert the chat won).
- `None` chat falls back to the configured target.
- `None` chat and `notify_target_configured()` returning `None` → `notify()` sends nothing
  (`cmd_send` not called), returns False, and reports once.
- A partial chat (`("telegram", "", "")`) is treated as no chat, not as `"telegram:"`.

---

## §2 — `core.py`: one line, and route it to the chat

**Current body**, verified live through the production path:

```
'♾️ **Refine Cycle** — new lesson learned\n\nmemory: refine-delivery-verification\n↩ undo: /refine-cycle rollback 582cdeaea333'
```

**Required body — exactly this, nothing else:**

```
♾️ Refine Cycle — new lesson learned
```

No markdown (`**` renders literally on some platforms), no blank line, no trailing newline, no
kind, no name, no journal id.

Change `_notify_lesson` (around line 3501) from
`_notify_lesson(*, kind, name, journal_id)` to
`_notify_lesson(active_chat=None)` and pass the chat into `_notify.notify(body, active_chat)`.

Keep the surrounding `try/except` and keep the comment explaining that the call happens **after**
the journal entry exists and **only** for `outcome == "applied"`. That ordering is still the
contract; only the body and the address change.

Update the single call site — the `if outcome == "applied":` branch, just after the
`ledger.record_edit` block (around line 5093). It currently passes `kind=kind, name=name,
journal_id=entry_id`.

### Dead code to check, not assume

`core._command_display()` and `core._set_command_display_provider()` exist **only** to render the
command name inside the old body. `register()` calls
`core._set_command_display_provider(_command_display_name)`.

Grep all three names. If the body is their only consumer, remove all three (including the
`register()` line) and say so in the commit. **If anything else uses them, leave them alone** — a
small unused helper is cheaper than breaking a live resolver.

### Existing assertion that must change

`tests/run_tests.py::test_applied_edit_notifies_once` asserts:

```python
self.assertIn("rollback", calls[0])
```

That is now wrong **by decision, not by accident**. Update it to the new contract and say so
explicitly in the commit message. **This is the only existing assertion you may change.** If any
other test fails, that is a signal you broke something — report it, do not edit it.

### Tests for §2

- The delivered body **equals** the required line — `assertEqual`, not `assertIn`. An `assertIn`
  cannot catch appended text, which is exactly what is being removed.
- Assert the absence of `**`, `rollback`, `undo`, and of a distinctive `name` you passed into the
  applied path. A future regression that re-adds the label must fail here.
- Still exactly **one** notification per applied edit.
- Still **zero** for `dry_run`, `no_op`, `rejected`, `prepared`, `error`, `pending_approval`,
  `llm_error`. The existing subTest covering this must keep passing untouched.

---

## §3 — `__init__.py` + `core.py`: carry the chat from hook to notification

This is the plumbing. It follows the **existing `session_ending` pattern** end to end — read how
`session_ending` travels before you start, and mirror it.

### 3a. The capture helper (`__init__.py`)

Add `_capture_active_chat() -> Optional[Tuple[str, str, str]]` near `_cooldown_elapsed()`.
Add `Tuple` to the `from typing import ...` line.

```
try:
    from gateway.session_context import get_session_env, session_is_messaging_surface
    if not session_is_messaging_surface(): return None
    platform  = get_session_env("HERMES_SESSION_PLATFORM", "")
    chat_id   = get_session_env("HERMES_SESSION_CHAT_ID", "")
    if not platform or not chat_id: return None
    return (platform, chat_id, get_session_env("HERMES_SESSION_THREAD_ID", ""))
except Exception:
    logger.debug(...); return None
```

- Import **lazily, inside the function**. `gateway.session_context` is host internals; a bare CLI
  process or a future host without it must degrade to `None`, never raise.
- The docstring must say: **call only from a hook callback**, and why.

### 3b. Thread it through — every site, none optional

`core.py`:

| Function | Change |
|---|---|
| `refine_run` (~5639) | add `active_chat=None` kwarg; pass to **both** `_refine_once` calls (the `if dry_run:` early return, and the one inside the `for _ in range(max_runs)` loop) |
| `_refine_once` (~3849) | add `active_chat=None` kwarg; pass to `_notify_lesson` |

`__init__.py`:

| Site | Change |
|---|---|
| `_AUTO_PENDING_SESSION_ENDS` (~25) | value becomes the tuple `(llm, active_chat)`; update the comment above it |
| `_defer_or_claim_session_end` (~40) | add third param `active_chat=None`; store `(llm, active_chat)` |
| `_finish_auto_worker` drain (~66) | unpack `session_id, (llm, pending_chat) = pending`; pass `_active_chat=pending_chat` to `_on_session_end` |
| `_on_session_end` (~1267) | add `_active_chat=_ACTIVE_CHAT_UNSET`; resolve via `_capture_active_chat()` **only** when the sentinel is present; pass into `_defer_or_claim_session_end` |
| `_collect_and_run` inside it | **two** `_run_auto_refine(...)` calls — both need `active_chat=active_chat` |
| `_run_auto_refine` (~212) | add `active_chat=None` kwarg; pass to `core.refine_run` |
| `_start_auto_refine` (~266) | add 4th param `active_chat=None`; pass to the thread via `kwargs={"active_chat": active_chat}` (**not** `args`, or it lands on `cleanup_session_notes`) |
| `_on_post_llm_call` (~686) | capture and pass as the 4th argument |
| `_handle_refine_command` | **three** `core.refine_run` calls: the dry-run branch, the `session <id>` branch, and the final prose/reason call. All three need `active_chat=_capture_active_chat()` |
| `_handle_refine_run` (tool) | same |

Use a **separate sentinel** `_ACTIVE_CHAT_UNSET = object()` rather than reusing `_BOUND_LLM_UNSET`.
Two unrelated parameters sharing one sentinel reads as a bug even when it works.

The command and tool handlers run **inside** the turn, so capturing inline at the call is correct
there. Only the hook→worker paths need the pass-down.

### 3c. Existing test that will break

`tests/run_tests.py` (~6463) wraps the claim function with a **fixed two-argument signature**:

```python
def observed_session_claim(session_id, llm):
    session_claiming.set()
    return original_session_claim(session_id, llm)
```

Adding a third argument breaks it with `TypeError`. Widen it to
`(session_id, llm, *args, **kwargs)` and forward them. This is a mock wrapper, **not** an
assertion — widening it changes nothing the test verifies. Say so in the commit message.

### Tests for §3

- A captured chat reaches the notification through the **auto** path (hook → thread → refine_run →
  notify) and the resulting target is the chat's.
- Same through the **command** path and the **tool** path.
- **The load-bearing one:** patch `gateway.session_context.get_session_env` to raise if called from
  any thread other than the capturing one, run an applied-edit auto path, and assert the
  notification still resolved from the captured triple. This is the test that protects the rule a
  later refactor will most likely break.
- A deferred session-end (the drain path) delivers to the chat its **original** callback captured,
  not to whatever the worker sees.
- Two concurrent runs with **different** captured chats each notify their own chat and never the
  other's. Per AGENTS.md a concurrency test must actually start two threads. This is a privacy
  test: a shared global would cross the wires here.
- `gateway.session_context` unimportable → `_capture_active_chat()` returns `None`, the run
  completes, tier 2 is used.
- A CLI turn (`session_is_messaging_surface()` False) → `None` even when stale
  `HERMES_SESSION_*` values are present in the environment.

---

## §4 — cleanup, only if §2's grep says it is dead

`config.notify_target()` and, if unused, `core._command_display` /
`core._set_command_display_provider` and the `register()` line that installs the provider.

Separate commit. If anything still uses them, skip this section entirely and say why.

---

## §5 — Order

§1 → §2 → §3 → §4.

§1 first because it is pure and testable with no host. §2 next so the exact-equality body test
exists before §3 starts moving addresses around. §3 is the plumbing. §4 last, and only if dead.

---

## §6 — Tracing on the live server (do this, not just the suite)

Green tests are a **secondary** signal. Every serious defect in this project so far was invisible
on synthetic input and obvious on real data — including the notification bug this spec follows,
which stayed green through an entire audit.

### Connection facts

| What | Value |
|---|---|
| Host | `ubuntu@92.5.18.124` |
| Key | `G:\Hermes\ssh-key-2026-06-20 (1).key` (quote it — it has a space) |
| Interpreter | `/home/ubuntu/releases/hermes-agent-v2026.8.31-clean/.venv/bin/python` |
| Hermes CLI | `/home/ubuntu/releases/hermes-agent-v2026.8.31-clean/.venv/bin/hermes` (**not** on `PATH`) |
| Plugin dir | `~/.hermes/plugins/refine` (a git checkout; deploy with `fetch` + `reset --hard`) |
| Journal | `~/.hermes/refine-data` |
| Logs | `~/.hermes/logs/{agent,gateway,errors}.log` |
| Service | `sudo systemctl restart hermes-gateway` |
| Registered as | `/refine-cycle` (a built-in `/refine` wins, so the fallback name is used) |
| `state.db` | `~/.hermes/state.db` — **open `mode=ro`, always** |

### The probe recipe — use this, PowerShell will mangle anything else

Inline `python -c "..."` over SSH **fails**: PowerShell eats the quotes and you get
`SyntaxError: unexpected EOF`. Base64 the script instead. This works:

```powershell
$py = @'
import sys
sys.path.insert(0, "/home/ubuntu/.hermes/plugins/refine")
import notify
print("target:", notify.target_for_chat(("telegram", "6667956926", "")))
'@
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($py))
ssh -i 'G:\Hermes\ssh-key-2026-06-20 (1).key' -o StrictHostKeyChecking=no -o BatchMode=yes `
  ubuntu@92.5.18.124 `
  "cd ~/.hermes && echo $b64 | base64 -d > /tmp/_p.py && /home/ubuntu/releases/hermes-agent-v2026.8.31-clean/.venv/bin/python /tmp/_p.py 2>&1 | grep -viE '^(DEBUG|INFO):'; rm -f /tmp/_p.py"
```

`cd ~/.hermes` matters — config resolution is relative to it. Always `rm` the probe.

### Trace 1 — simulate an active chat without waiting for a real turn

`get_session_env` falls back to `os.environ` when the ContextVar was never set, so a bare probe can
stand in for a live turn by exporting the same names. Put this **before** the imports in your probe:

```python
import os
os.environ["HERMES_SESSION_PLATFORM"] = "telegram"
os.environ["HERMES_SESSION_CHAT_ID"]  = "6667956926"
os.environ["HERMES_SESSION_THREAD_ID"] = ""
```

Then assert `_capture_active_chat()` returns `('telegram', '6667956926', '')` and that
`target_for_chat` turns it into `telegram:6667956926`. Also run it **without** those vars and
confirm you get `None` (a CLI surface).

### Trace 2 — a real delivery through the production call path

Exercise `core._notify_lesson` itself, not a hand-built body, so you are testing the shipped path:

```python
captured = {}
real = core._notify.notify
def spy(body, chat=None):
    captured["body"], captured["chat"] = body, chat
    captured["ok"] = real(body, chat)
    return captured["ok"]
core._notify.notify = spy
core._notify_lesson(active_chat=("telegram", "6667956926", ""))
print("BODY_REPR:", repr(captured["body"]))
print("DELIVERED:", captured["ok"])
```

Expected: `BODY_REPR: '♾️ Refine Cycle — new lesson learned'` and `DELIVERED: True`. A real Telegram
message arrives — that is the point. Confirm the `repr` is the single line with **no** `\n`.

### Trace 3 — the end-to-end, through Hermes for real

Deploy, restart, then drive it from a real chat:

```powershell
ssh -i 'G:\Hermes\ssh-key-2026-06-20 (1).key' ubuntu@92.5.18.124 `
  "cd ~/.hermes/plugins/refine && git fetch -q origin main && git reset --hard origin/main -q && git log --oneline -1 && git status --short"
ssh -i 'G:\Hermes\ssh-key-2026-06-20 (1).key' ubuntu@92.5.18.124 `
  "sudo systemctl restart hermes-gateway && sleep 12 && systemctl is-active hermes-gateway"
```

Then, **from Telegram**, send `/refine-cycle status` to confirm the plugin answers, and
`/refine-cycle dry-run` to confirm a proposal round-trips. A dry run must **not** notify. To see a
real notification you need an applied edit — check the budget first (`edits_today` in
`/refine-cycle status`; the cap is 10).

Read the log after:

```powershell
ssh -i 'G:\Hermes\ssh-key-2026-06-20 (1).key' ubuntu@92.5.18.124 `
  "grep -h 'refine notify' ~/.hermes/logs/*.log | tail -5; echo '(no lines above = no failed delivery)'"
```

A `refine notify: could not deliver` WARNING means the address was wrong — read it, it names the
target and the exit code.

### What to state when you are done

Per section: reproduced or not, the fail-first output you **read**, behaviour fix or hardening. Plus
the resolved target you observed in Trace 1, the `repr` from Trace 2, and whether Trace 3's message
actually arrived. If a path could not be exercised, **say so** — do not imply coverage you do not
have.

---

## Stop condition

- Each section committed separately and pushed; author
  `263254659+Bergschloss@users.noreply.github.com`.
- Suite green after each commit, count read and stated.
- `py_compile` clean, `git diff --check` clean, no `__pycache__`, no probe files, `/tmp` probes
  removed from the server.
- CI green **4/4 on your final SHA**. CI cancels in-progress runs, so only the last SHA matters:
  `gh run list` then `gh run view <id> --json jobs`.
- Server checkout clean at your final SHA, gateway restarted and `active`.

**Stop and report instead of improvising if:** a test other than the two named ones fails; the
"never call from a worker" test cannot be made to pass; removing `_command_display` breaks
something unexpected; or any fix would require loosening a guardrail.
