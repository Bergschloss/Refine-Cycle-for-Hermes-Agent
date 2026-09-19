# Usage

Split out of the README.

## Usage

### What to expect

A pass on quiet data is a `no_op` — that is the normal, correct result, not a
failure. The outcome families are `no_op`, `applied`, `rejected`,
`pending_approval`, `conflict`, `llm_incomplete`, `llm_invocation_unavailable`,
and `failed`, plus the rollback and grading terms in `/refine audit`.

A real 90-day history (the long-running install this README was tested
against) shows eight refine-created entries whose effectiveness verdicts
distribute between `too early`, `rolled back`, `rejected`, and
`unreliable` — with `unreliable` meaning *someone else modified the artifact
after refine touched it*, so no verdict is possible. Expect exactly that mix:
most passes doing nothing, some edits reverting, and very few edits surviving
to a `working` verdict.

### How this differs from Hermes's built-in self-improvement

Hermes ships its own background review: after a turn or a session it looks at the
current conversation and saves what is worth keeping — a useful tactic, a user
preference, a correction. It answers **"is there something here worth
remembering?"**

**Refine Cycle** answers a different question, over a different window, and then
checks its own work:

| | Hermes background review | **Refine Cycle** |
|---|---|---|
| **Trigger** | anything worth keeping | proposal signal at 2 repeats; application only at 2 sessions **or** 5 occurrences |
| **Window** | the current session | many sessions |
| **Evidence** | the conversation as written | errors normalized to invariant shapes and fingerprinted, so `HTTP 429 for /users/8821` and `HTTP 429 for /users/9134` count as one failure |
| **Threshold** | qualitative judgement | a cheap proposal gate followed by an application bar: distinct-session count **or** occurrence count |
| **After the edit** | — | grades it: `working`, `did not help`, `unused`, `churning`, or names honestly why no verdict exists yet (`too early`, `no recurrence window`, `unreliable`) |
| **Blast radius** | host policy | 3 edits/day, dedup window, cooldown, per-edit journal, per-edit rollback |

The two are complementary, not alternatives. Hermes captures fresh experience;
**Refine Cycle** hunts chronic failures and measures whether its own fixes held.

Both can write to the same skills and memory, so the plugin is built to notice
that: a skill patch is refused outright when the target changed after planning,
and `/refine audit` reports when an entry it created was modified by something
else, because an effectiveness verdict on a file someone else edited is not a
verdict worth trusting.

### Manual

The examples below use `/refine`. If the Hermes host already owns a built-in
command with that name, the plugin registers as `/refine-cycle` instead; the
registration warning and command help show which name is active.

```
/refine
/refine focus on Gmail API failures
/refine audit
/refine status
/refine update
/refine dry-run
/refine dry-run focus on Gmail API failures
/refine dry-run session <session_id>
/refine session <session_id>
/refine model
/refine model your-cheap-model
/refine model your-provider/your-cheap-model
/refine model auto
/refine rollback 1f2a3b4c5d6e
```

`audit`, `status`, `dry-run`, `model`, `session <session_id>`, and
`rollback <12-character-id>` are exact subcommands. `status` reports whether
automatic refinement is active, which session and database source would be
analyzed, configured source skips, what blocks refinement, which model it will
use, and the active journal/migration state. `dry-run [reason]` runs the normal
proposal path and journals the preview without applying an edit or consuming the
daily edit budget. `dry-run session <session_id>` previews one exact historical
session after confirming it through the read-only Hermes sessions table.

`update` installs the latest release when it is newer than the installed one. It downloads the commit the release tag points at, not a branch tip, runs the `install.py` shipped in that release with `--plugin-only`, and checks that the plugin directory now reports the new version. If anything fails, the previous files are put back. When the plugin is already current, the command still checks the host: if a Hermes update removed the route patch, it asks the installer to put it back, and says so when no bundled patch fits this Hermes. After an update or a repair it restarts Hermes itself: inside the gateway through the gateway's own restart (the same one `/restart` uses), scheduled a few seconds after the reply; from a CLI it runs `hermes gateway restart` when a gateway is running. An install from the Hermes plugin catalog is left alone and the reply points to `hermes plugins update refine-cycle`. The plugin never runs this on its own. `/refine_update` and `/refine_fix` run the same command in one tap.

That restart is the gateway's own (`request_restart`): it stops taking new turns, waits for turns in progress up to `restart_after_turn_timeout`, then stops what is still running (a long task, a subagent, a turn past the timeout). Sessions are kept in `state.db` and continue after the restart. The desktop button restarts the desktop backend straight away, which cuts off a reply the app is still receiving. So an update is a deliberate interruption, and it only ever starts from the user's own tap or command.

`model` shows or sets the model refine asks for. Bare `model` prints the
effective target and whether host trust allows it; `model <name>` or
`model <provider>/<name>` pins one; `model auto` removes the override. `auto`
returns to the next source in the priority order, which is the configured
`plugins.entries.refine.llm` value when there is one, and the live Hermes model
only when there is not. The override is stored in `model_override.json` inside
`journal_dir` — refine does not put its own settings in the Hermes config. It
writes there exactly once, for one key that is not its own: see below.

Both stores are validated the same way: a provider must be a single token, a
model id may be namespaced, and a value matching a credential pattern is refused
rather than stored. A configured value that fails either rule is dropped and
reported in `/refine status` and `/refine model`.

In the command, **the first slash is always the provider separator** and every
later one belongs to the model id: `/refine model openrouter/deepseek/deepseek-chat`
pins provider `openrouter` and model `deepseek/deepseek-chat`. There is therefore
no command form for a namespaced model with no provider — set
`plugins.entries.refine.llm.model` for that. And a pinned provider only reaches
the host when `allow_provider_override` is true, which `/refine model` reports.

Other text is passed to the proposal model as the manual reason. That includes
text beginning with a subcommand word, with one deliberate exception: after
`model`, a single token shaped like an identifier (`deepseek-v4`, `a/b`) is
treated as a target, so `/refine model drift` pins a model rather than asking for
a refinement about drift. Use `/refine drift` or `/refine model auto` to undo.

### Messages from Refine Cycle

Every message starts with `♾️ Refine Cycle` and is sent once per event, to the last chat you talked to Hermes from (or `notify_target`):

| When | Message |
|---|---|
| A new release is out (checked once a day across all Hermes processes) | `♾️ Refine Cycle — update available: 1.3.12.` + `/refine_update — Hermes will restart.` |
| A Hermes update stopped the plugin | `♾️ Refine Cycle stopped working after the Hermes update.` + `/refine_fix — Hermes will restart.` |
| No bundled patch fits the new Hermes | `♾️ Refine Cycle is paused: Hermes 0.22.0 isn't supported yet. You'll get a message when it is.` |
| Hermes restarted on the new version | `♾️ Refine Cycle 1.3.12 is running.` |
| Hermes restarted after a fix | `♾️ Refine Cycle is working again.` |
| A desktop app on this host has the plugin's desktop half, switched off (once per host) | `♾️ Refine Cycle — please turn on the plugin: Capabilities → Plugins → Refine Cycle → Desktop — switch ON.` |
| A lesson was refused because memory is full | `♾️ Refine Cycle: memory is full (7998/8000). Lesson not saved. Remove old entries or raise memory_char_limit.` |

`/refine status` starts with one line in the same words: `working` with memory use, `update available` with `/refine_update`, or `not working` with `/refine_fix`. On Telegram a command written with underscores is one tap; the gateway maps it to the registered `refine-update` / `refine-fix`.

**In the Hermes desktop app** the plugin's desktop half (`desktop/plugin.js`) puts one item in the status bar: `♾️ Refine Cycle 1.3.12 · working`, `· update available: 1.3.13` with an **Update** button, or `· not working` with a **Fix** button. It sends no system notifications: what it has to say it says in chat, and a second copy in the corner of the screen is noise. Pressing it starts the work in the plugin's backend (Hermes stops waiting for a plugin command after 30 seconds, and an install takes longer), shows `♾️ Refine Cycle updated to 1.3.13. Restarting Hermes…` when it is done, and restarts the desktop backend, as Settings ▸ Restart backend does. Hermes loads a plugin's desktop half switched off; turn it on once on the Plugins page.

**A button inside the chat**, not only in the status bar. A plugin cannot post into the desktop chat (Hermes renders a card only inside an assistant message, and a plugin's own output is a plain-text `system` message), so the plugin puts it under the agent's own reply. When there is something to say, a new release or a Hermes update that stopped the plugin, the next reply in the desktop app ends with a card showing the state and the **Update** or **Fix** button. It goes under one reply per event, wired straight to the plugin's backend: no HTML file, no hidden turn, no model call for the press. The desktop half claims the `::refine` directive for this, so the agent can also show the card when you ask it to write `::refine{}`. With the desktop half still switched off, the card would show as raw text, so that reply instead carries the "please turn on the plugin" line, once. A desktop app too old to render transcript directives gets no card at all; its status bar carries the same Update / Fix. A reply cut off inside a code block gets no card either, since the block would swallow it; the card goes under the next reply.

The button is the supported way to update from the desktop app or a TUI, because the 30-second bound applies to a *typed* `/refine-update` too: Hermes stops waiting for the answer, the session reports a timeout, and the update itself carries on and restarts Hermes without ever printing its reply. In a chat the command is awaited for as long as it needs, so the one-tap message path is unaffected.

What was already said lives in `notices.json` in the journal directory. `update_check_enabled: false` turns the release check off.

### Automatic refinement

Automatic refinement is **enabled by default** (`auto_enabled: true`). After
enabling the plugin and restarting Hermes, it begins analyzing sessions and
proposing improvements without additional configuration. To disable it, set
`auto_enabled: false` in `plugins.entries.refine`.

`post_llm_call` counts the assistant messages in the history Hermes supplies and
starts at most one background refinement attempt once that count has grown by
`auto_turn_interval` since this session's previous attempt. It compares a delta
rather than an exact multiple, because a single tool-using turn appends several
assistant messages and would otherwise step straight over the boundary. The hook
itself does not mutate or queue work. It skips an attempt when another pass owns
the lock, and derives its cooldown from durable journal records, so the cooldown
is visible across processes. `on_session_end` remains a background fallback based
on the minimum message count.

```yaml
plugins:
  entries:
    refine:
      auto_enabled: true
      auto_min_messages: 15
      auto_turn_interval: 25
      auto_cooldown_minutes: 20
```

Automatic and manual runs share a cross-thread and cross-process mutation lock,
then recheck the daily budget inside that lock.

### Reviewer fallback

When `min_signal_required` is enabled but the mechanical gate finds neither a
repeated pattern nor an explicit correction, a substantial session can receive
one structured reviewer call (`max_tokens: 2400`, timeout 180 seconds). It asks
only whether there is a durable lesson worth persisting. The reviewer has its
own cooldown.

A reviewer decline, malformed verdict, or reviewer error never reaches the
proposal call. Declines are recorded as sanitized `no_op` journal entries so
they can be audited. An approval supplies narrow instructions to the normal
proposal flow but remains advisory: it is journaled as `reviewer_only` and is
never applied without the ordinary recurrence evidence.

### Prompt notes and scope

A `prompt` proposal creates a short conditional policy in
`<journal_dir>/prompt_notes.json`. Valid notes contain one or two policy lines
beginning with `When <specific condition>, <one action>.`; they are not skills,
memories, procedures, or system-prompt replacements.

`pre_llm_call` returns a self-labelled `Refine notes:` context block. Hermes
adds that ephemeral context to the current turn; **Refine Cycle** never reads or
writes the base system prompt. Injection is bounded by
`prompt_notes_max_count` and `prompt_notes_max_chars`; when necessary it drops
whole oldest notes, never partial text. Empty, unavailable, unsafe, or
out-of-scope note stores inject nothing and do not raise on the user path.

Injection prefers the mutation lock but does not depend on it: the store is only
ever replaced atomically, so a running refine pass never costs a turn its notes.

A note of the form `When <condition>, use X instead of Y.` also becomes a block
rule: a tool call named `Y` and a terminal command running `Y` are refused with
the note's text. Hermes does not tell the plugin which tools a turn has, so a
bare name closes both. Core tools (`read_file`, `terminal`, `memory`, …) and
load-bearing binaries (`git`, `python`, the shells, package managers) are never
blocked, whether the note is new, old, or edited by hand.

New prompt notes use `prompt_notes_default_scope`:

- `global` (the default) is injected in every session.
- `session` stores the session identifier resolved while reading `state.db` and
  injects only when the hook receives that same identifier. Session notes are
  removed from the plugin-owned store after `on_session_end` or
  `on_session_reset` for that session.

Cleanup runs on the host's callback thread, so it waits only briefly for the
mutation lock instead of the full lock timeout. If a refine pass still owns the
lock, the note is left in place — it can no longer be injected, because its
session is gone — and it is removed at the next end or reset for that id.

That expiry is itself journaled, so a crash cannot turn "the note landed and was
then cleaned up" into "the note never landed": the entry moves `applied` (or
`prepared`, for a note that landed before its own finalization completed) →
`cleanup_prepared`, fsynced *before* the store changes, and only reaches
`cleanup_resolved` once the exact note is proven absent from a fresh read. Both
states count against the daily budget, because the edit really happened — normal
session expiry is not a refund and not rollback evidence. Consequently a
session-scoped note stops being reversible once its session ends: `/refine
rollback <id>` then reports the entry as not reversible, since the artifact it
would remove is already gone. Ledger rows for the two states read *session
cleanup pending* and *session note expired*.

Cleanup removes only a note whose id, content, scope, and session still match
the intent recorded in the journal. A note that was hand-edited or moved to
another scope or session is **retained** and reported by id, and an entry already
at `cleanup_prepared` stays there until the store is repaired. That is
deliberate — refine does not delete what it cannot prove it owns — but it does
not clear itself; see *Known integration gaps*.

The prompt-note store is plugin-owned, so there is **no host approval gate** for
these notes. Creation, target-state proof, audit rows, and conflict-aware
rollback are still journaled; host approval remains in force for host-managed
skills and memory.

### Host write approval is turned off on load

If `memory.write_approval` or `skills.write_approval` is on, refine sets it to
`false` when it registers, logs a warning naming what it changed, and leaves a
copy of the previous file at `config.yaml.refine-bak`.

That is a deliberate exception to "refine does not write to the Hermes config",
and it exists because the gate does not do what its name suggests to an
autonomous plugin. It queues **every** memory and skill write — the agent's own as
much as refine's — and nothing lands until a human drains the queue by hand.
Nothing reports that. It presents as an agent that quietly stopped learning:
memory unchanged, skills missing, no error anywhere. In one real install it ran
that way for days, with 3 memory writes and 25 skill writes stranded and four
skills the agent believed it had saved absent from disk.

The write is the narrowest one possible: only a `write_approval: true` line inside
the `memory:` or `skills:` block is rewritten, so comments, key order and every
other value survive. The same key under any other section is left alone, and a
config pinned by an administrator (managed scope) is never touched — there refine
only warns. `/refine status` reports the gate whenever it is on, so re-enabling it
later is visible rather than silent.

If you want approval gating on those subsystems, disable refine instead of turning
the gate back on; the two are answers to the same question and only one of them
can win.

### How a memory entry is identified for rollback, and one disclosed gap

Adding a memory entry goes through the host's gated memory tool, so with
`memory.write_approval` enabled it stages as `pending_approval` like any other
gated write. **Removing it does not go through that gate**, and that is a
deliberate trade rather than an oversight.

The host's removal identifies an entry by *substring*, and pops a single match
even when that match is a strict superstring of the text it was given. Under the
gate a removal is staged and replayed later, so between staging and approval the
entry can be replaced or extended — and the replay would then delete the **user's**
entry. That is a delete of something refine never created, which this plugin may
never do, and it would outrank the value of the gate.

So refine removes its own append itself: it re-reads under the host's per-file
memory lock, proves the entry is its own — exact content, at or after the position
recorded when the edit was planned, with everything before that position pinned by
a digest — and deletes only that entry, all inside the lock. If its exact text is
no longer there, rollback refuses and removes nothing, and the entry stops being
advertised as reversible. A longer entry that merely contains refine's text is not
a problem: identification is by exact content, not substring.

Two consequences worth knowing:

- A memory rollback is not reviewable through `memory.write_approval`. Skill
  rollback does stage under `skills.write_approval` — but note that staging does
  not make it safer in this respect: the host replays a staged skill delete by
  name, without re-checking content, so a skill edited during the approval window
  is deleted as approved. Rolling back a refine-created skill while skill write
  approval is on is best done promptly, or not at all if the skill has since been
  edited by hand.
- With the gate on and an interactive prompt registered, the *forward* memory
  write can block on that prompt while the refine pass holds the shared mutation
  lock, so a concurrent `/refine` waits out its lock timeout and the automatic
  session-end pass skips that round.

One ambiguity remains and is not solvable from the host API: an entry written by
something else that is byte-identical to refine's own. The host refuses exact
duplicates, so this requires another writer reproducing refine's scrubbed text
verbatim.

### When other writers rewrite MEMORY.md, and when it is full

Refine is not the only writer of `MEMORY.md`. The host consolidates it, and some
memory stacks rebuild it on their own schedule, so an entry refine applied can be
gone the next day while its journal row still says `applied`. The journal is never
rewritten to match; instead, before every model call refine reads the live store
and checks its own past memory edits against it:

- A lesson still in the store covers its failure, and that failure is not offered
  to the model again. This coverage does not age the way prompt-note coverage does:
  only the last `prompt_notes_max_count` notes cover anything, while a memory entry
  covers its failure for as long as its exact text is in `MEMORY.md`, whatever the
  audit later says about whether the lesson helped. If a lesson did not work, the
  way to let refine try something else for that failure is to remove the entry.
- A lesson no longer in the store covers nothing. The model sees it in its history
  as `not_in_memory` rather than `applied`, and the same text may be written again
  instead of being refused as a duplicate.
- The prompt states how many characters a new memory entry can hold, and says so
  plainly when the store is full.
- When a memory lesson was refused because the store was full, its failure is not
  offered again while the store has no more room than it had at that refusal.
  Removing an entry or raising `memory_char_limit` makes it eligible again.

A pass that drops failures for these reasons journals the counts as
`memory_live_covered` and `memory_full_backoff`, and when nothing is left to offer
it ends as `no_applicable_pattern` without calling the model.

Membership is by exact text, the same limit rollback has: an entry another writer
reworded counts as gone.

### Multi-edit transactions

Some lessons are not one edit. A new skill and the memory entry that says when to
reach for it are inseparable: applied separately, the state between them is
inconsistent. A proposal may therefore carry an `edits` array under one shared
reason, `expected_outcome`, and `summary`, capped by `max_edits_per_proposal`.

Durably, nothing new was invented. Each edit still gets its own journal record,
its own recovery metadata, and its own rollback ID, tied together only by an
additive `group` field (`id`, `index`, `size`, `summary`, and `dropped` when
edits were discarded). That is what keeps `/refine rollback <id>`, approval
reconciliation, dedup, and the ledger working exactly as before — and it is why
the daily budget counts edits rather than proposals.

Edits apply in order and the run stops at the first failure. A partial
transaction is never reported as clean:

- Applied and reserved edits are `applied` / `pending_approval` as usual.
- An edit whose host write landed but whose journal finalization failed still
  owns a recovery ID and is listed as one.
- Edits the daily budget refused, and edits not attempted after an earlier
  failure, are journaled as `rejected`, which consumes no budget.
- Edits discarded while shaping the proposal — past the cap, unusable, or
  repeating a target already claimed in the same proposal — are counted, block a
  `completed` verdict, and are reported in `group.dropped`.

So which edits of a transaction landed is readable from the journal alone, not
only from a message that automatic runs discard.

There is no `delete` action: a transaction can only create or patch.

### Auditing what refine wrote

`/refine audit` reports whether refine-created entries were used and whether the
failure fingerprint recurred after the edit. Timestamp-aware host counts are
preferred. If the host exposes only an all-time aggregate, the report labels it
`all:` and does not claim post-edit use from it. Pending approvals remain marked
as pending rather than applied. On the next audit, run, or rollback request, the
plugin checks the host pending store and actual skill or memory target: an exact
target match becomes applied, an unresolved host record stays pending, and a
removed host record without a target match becomes rejected.

```
Refine-created entries (3):

  name                           age  ver     uses  recurred  verdict
  gmail-scope-fix                12d   v2        5        no  working
      expects: Gmail sends stop returning insufficient_scope
  prisma-migrate-note             9d   v1       ~0         —  too early
      expects: —
  bash-path-hint                  3d   v3        2       yes  did not help
      expects: PATH errors stop appearing before shell commands

Candidates for removal:
  bash-path-hint — /refine rollback 8c1d2e3f4a5b
```

The audit deletes nothing. It prints a rollback command only for recorded
candidates. Skill rows keep their plain names; memory and prompt-note rows use
`memory:` / `prompt:` prefixes so same-named entries remain distinguishable.
Every row shows the model's sanitized expected outcome (`—` when omitted)
alongside its observed result. Later edits of the same entry advance a version;
version 3 or later is labelled `churning` only when the normal verdict would
otherwise be `unclear`. Skills that remain unused are fed into later proposals
as negative examples.

Two honesty rules behind the verdicts:

- **`no recurrence window`** — the pattern table had no post-edit rows at all
  (typically after a restored or rebuilt `state.db`). An empty scan cannot
  tell "the failure stopped" from "the evidence was lost", so the row names
  the gap instead of drifting into `unclear` or claiming `working`.
- **Recurrence horizon** (`refine.audit_recurrence_horizon_days`, also accepted
  as `refine.recurrence_horizon_days`, default **3**).
  On the reference journal, the median gap between recurrences of a chronic
  failure is minutes and the 95th percentile is 2.17 days — so silence shorter
  than the horizon is indistinguishable from a pause. Fingerprintless rows
  (no recurrence signal at all) earn `working` only after `age >= horizon`;
  edits younger than that stay `too early`. Raise the key only if your
  failures genuinely pause longer than that; the default is measured, not
  guessed. This horizon governs recurrence verdicts only — `unused_skills`'
  separate `min_age_days` (14) answers a different question ("has the skill
  been left idle") and is unchanged.
- **A kind with no usage counter still earns `working` — on recurrence alone.**
  The host counts uses only for skills, so `uses` is structurally unavailable for
  memory entries and prompt notes. Until recently that made `working` unreachable
  for them: the branch required `uses > 0`, so the edit kind refine produces most
  often could never be reported as successful however long it held, and the column
  read `unclear` forever. Recurrence now carries the verdict alone for those kinds,
  under the same bar the usage path uses and not a lower one — the silence must be
  *measured* (`recurred` false, never unmeasured), a fingerprint must exist (with
  neither a fingerprint nor a counter there is no evidence at all, and the row stays
  `unclear`), and the edit must be older than the recurrence horizon. The row still
  prints `uses` as `—`, so it stays visible which evidence carried the verdict. The
  gate is on **kind**, not on `usage_scope`: a skill whose usage lookup merely
  *failed* also reports `unavailable`, and that is an unmeasured dimension rather
  than an absent one, so it does not borrow this path.
- **Memory rows check presence, not usage.** The host keeps no usage counter
  for memory entries, so the only checkable fact for an applied memory edit is
  whether the exact content refine appended is still in the store. Exact
  membership cannot tell an edit from a removal — both make the string
  disappear — so when the content is gone the verdict is
  `unreliable — no longer present as applied`, never "was deleted". If the
  host memory state cannot be read at all, the row says
  `unreliable — target state unavailable` rather than guessing.

### Agent-invocable tool

The agent gets a `refine_run` tool (toolset `refine`) and may trigger the same
serialized flow with an optional `reason`. It also accepts `session_id` for one
exact historical session and `dry_run: true` to preview without applying. The
handler validates an explicit session against the read-only sessions table
before any model call and forwards all three arguments to `core.refine_run`.

The tool must run inside an active Hermes gateway turn: it reuses the
host-provided `ctx.llm`, which carries that turn's active runtime routing. An
external script that constructs `PluginLlm(plugin_id="refine")` is not
equivalent; outside a gateway turn it can fall back to a configured provider
instead of the active model.

### Which model refine uses

By default refine inherits the user's **live main model**. Hermes resolves the
model inside its own `call_llm`: with no explicit provider/model it takes the
`auto` path, whose first step is "main provider + main model", and the main
model is read from a process-local runtime override that the agent refreshes at
the top of every turn. So a model switched mid-session is intended to apply to
refine as well, without any plugin-side plumbing.

One caveat is worth knowing, and it depends on the Hermes version. On Hermes
builds older than 2026-07-17, auxiliary clients are cached under a key that does
**not** include the resolved model; a plugin call passes no live-runtime dict, so
the key is constant and the first cached client keeps supplying the model
captured when it was built, outliving a mid-session switch until the entry is
evicted or the process restarts. Upstream closed this in `73057ed16`
("scope runtime state to each turn") and `fdc6c32d7` ("isolate runtime cache by
live context"), both dated 2026-07-17 — verified by reading the Hermes repository,
not from this one, so re-check against your own checkout before relying on it.
Never a refine bug either way; on an older host, restarting the gateway clears it.

`/refine model` reports which source **refine** resolved, and with `source: live`
the value it read from the host at that moment. It cannot report which model a
cached host client will actually use, so on an older host it is not a way to
confirm a mid-session switch took effect. Restart the gateway, or pin the target.

Pinning refine's own target sidesteps all of that and makes the choice
deterministic:

```yaml
plugins:
  entries:
    refine:
      llm:
        allow_provider_override: true   # required for `provider` below
        allow_model_override: true      # required for `model` below
        provider: your-provider
        model: your-cheap-model
```

Model availability depends on provider, account, and region. A `403 RegionError`
means the provider received the request and refused that model — commonly an
account or region restriction that needs an explicit opt-in with the provider.
Because refine inherits the live main model, a restricted main model makes refine
fail for as long as the main model does; the fix is to opt in or select an
available model with `hermes model`, not to pin refine elsewhere. Check
`llm_meta.reported_provider` and `reported_model` to see which target was
actually refused.

Both `allow_*` flags are fail-closed in Hermes: with them off, a pinned value is
refused rather than applied. Leave `provider`/`model` unset to inherit the live
main model as described above. Every path — the `/refine` command, the
`refine_run` tool, and both automatic triggers — shares the one host-provided
client and honors this setting identically.

---

